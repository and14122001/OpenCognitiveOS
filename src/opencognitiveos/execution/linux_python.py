"""Linux sandbox broker for controller-approved, committed single-file Python tools."""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time
import uuid

from opencognitiveos.contracts.models import EffectOutcome, EffectReceipt
from opencognitiveos.errors import PolicyDeniedError, ReconciliationRequiredError
from opencognitiveos.verification.acceptance import AcceptanceSpecRegistry
from opencognitiveos.verification.evidence_store import EvidenceKind
from opencognitiveos.verification.seals import spec_seal


def execute_python(runner, request):
    from opencognitiveos.execution.sandbox import SandboxExecutionResult, SandboxPolicyError, SandboxUnavailableError
    gate = runner.authority.evidence_completion
    if (gate is None or gate.manager is not runner.workspace_manager or len(request.command) != 2
            or request.command[0] != "sandbox.python" or request.network_mode != "DENY"
            or request.extra_env or request.env_whitelist or not 0 < request.timeout_seconds <= 30):
        raise SandboxPolicyError("Unsupported sandbox command, environment or time limit.")
    path = request.command[1]
    task = runner.authority.get_task(request.task_unique_key).spec
    fence = dict(worker_id=request.worker_id, execution_id=request.execution_id, epoch=request.fencing_epoch)
    runner.authority.policy_gate.check_tool(task, "sandbox.python", args={"path":path})
    registry = AcceptanceSpecRegistry(runner.authority)
    spec, spec_hash = registry.load_and_verify_spec(task_spec=task, expected_policy_hash=gate.policy_hash)
    if spec.criteria.get("worker_script") != path:
        raise PolicyDeniedError("Script has no task-specific controller approval.")
    with runner.authority.store.open_connection() as conn:
        tag = conn.execute("SELECT tag FROM acceptance_seals WHERE digest=?", (spec_hash,)).fetchone()
    if tag is None or not hmac.compare_digest(tag[0], spec_seal(gate.controller_key, spec_hash, task)):
        raise PolicyDeniedError("Invalid script authorization.")
    # Read by immutable Git object, never open a mutable workspace path as root.
    from opencognitiveos.execution.path_guard import checked_path
    checked_path(request.workspace_path, path)
    ref = f"{task.base_sha}:{path}"
    manager = gate.manager
    if manager._run_git("cat-file", "-t", ref).stdout.strip() != b"blob":
        raise PolicyDeniedError("Approved script is not a Git blob.")
    if int(manager._run_git("cat-file", "-s", ref).stdout) > 1048576:
        raise PolicyDeniedError("Approved script is too large.")
    script = manager._run_git("cat-file", "blob", ref).stdout
    operation = "sandbox.python/" + spec_hash
    intent = runner.authority.record_tool_intent(task.unique_key, tool="sandbox.python", operation_id=operation,
                    args={"path":path, "sha256":hashlib.sha256(script).hexdigest(), "workspace_id":request.workspace_id},
                    approval_ref=spec_hash, broker_capability=runner.authority._broker_capability, **fence)
    if not intent.should_execute:
        raise ReconciliationRequiredError("Sandbox intent already exists; never redispatch.")
    unit = f"ocos-worker-{uuid.uuid4().hex}.service"
    limit = math.ceil(request.timeout_seconds)
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp)/"candidate.py"
        candidate.write_bytes(script)
        info_file = Path(tmp) / "namespace.json"
        info_file.touch(mode=0o600)
        launcher = Path(__file__).with_name("launch_python.sh")
        cmd = ["/usr/bin/sudo","-n","/usr/bin/systemd-run","--expand-environment=no","--quiet",
               "--unit="+unit,"--wait","--collect","--pipe","-p","MemoryMax=128M","-p","TasksMax=16",
               "-p","CPUQuota=50%","-p",f"RuntimeMaxSec={limit}","-p","TimeoutStopSec=1",
               "-p","KillMode=control-group","-p","LimitCORE=0","/bin/bash",str(launcher),str(candidate),str(info_file)]
        env = {"PATH":"/usr/bin:/bin","LC_ALL":"C"}
        result = bytearray()
        timed_out = False
        with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env) as proc:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    while True:
                        runner._authorize_lease(request)
                        elapsed = time.monotonic() - started
                        if elapsed >= request.timeout_seconds:
                            timed_out = True
                            break
                        if not selector.select(min(0.2, request.timeout_seconds-elapsed)):
                            continue
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        if len(result)+len(chunk) > 1048576:
                            raise PolicyDeniedError("Sandbox output quota exhausted.")
                        result.extend(chunk)
                if not timed_out:
                    proc.wait(timeout=5)
            finally:
                if proc.poll() is None:
                    # Never use a worker-supplied PID or PGID.
                    subprocess.run(["/usr/bin/sudo","-n","/usr/bin/systemctl","stop",unit],
                                   env=env, capture_output=True, timeout=8)
                    proc.wait(timeout=8)
                proc.stdout.close()
            timed_out = timed_out or time.monotonic() - started >= request.timeout_seconds
            code = 124 if timed_out else proc.returncode
        if not info_file.stat().st_size or not json.loads(info_file.read_bytes()).get("child-pid"):
            raise SandboxUnavailableError("Native namespace setup unconfirmed: " + result.decode("utf-8","replace")[:300])
    # Missing/unknown systemd state is not a quiescence certificate.
    show = subprocess.run(["/usr/bin/systemctl","show",unit,"--property=LoadState,ActiveState,SubState"],
                          env=env, capture_output=True, text=True, timeout=5)
    properties = dict(line.split("=",1) for line in show.stdout.splitlines() if "=" in line)
    if properties.get("ActiveState") != "inactive" or properties.get("SubState") != "dead":
        raise ReconciliationRequiredError("Worker unit quiescence not confirmed.")
    runner._authorize_lease(request)
    log = gate.cas.store_bytes(bytes(result), kind=EvidenceKind.EXECUTION_LOG)
    outcome = EffectOutcome.SUCCEEDED if code == 0 and not timed_out else EffectOutcome.FAILED
    runner.authority.record_effect_receipt(task.unique_key, EffectReceipt(intent.intent.intent_id, outcome, unit, log.digest),
                broker_capability=runner.authority._broker_capability, **fence)
    return SandboxExecutionResult("linux-bwrap", True, code, timed_out, False, True,
                result.decode("utf-8","replace"), "", None, time.monotonic()-started, log.digest)
