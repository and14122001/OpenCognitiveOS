"""Independent read-only native verification for a registered fixed-commit file check."""
import hashlib
import hmac
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from opencognitiveos.errors import VerificationFailedError
from opencognitiveos.execution.path_guard import checked_path
from opencognitiveos.verification.acceptance import AcceptanceSpecRegistry
from opencognitiveos.verification.evidence_store import EvidenceKind
from opencognitiveos.verification.manifest import (
    EvidenceManifestRegistry, ExecutionEnvironmentRecord, capture_trusted_git_state,
)
from opencognitiveos.verification.seals import spec_seal


def verify_file(authority, *, task_key, workspace_id, worker_id, execution_id, epoch):
    gate = authority.evidence_completion
    if gate is None or gate.manager.authority is not authority:
        raise VerificationFailedError("Trusted controller configuration required.")
    manager = gate.manager
    fence = dict(worker_id=worker_id, execution_id=execution_id, epoch=epoch)
    identity = manager.authorize_workspace(workspace_id, **fence)
    task = authority.get_task(task_key).spec
    if identity.task_unique_key != task_key or identity.base_sha != task.base_sha:
        raise VerificationFailedError("Worktree and task differ.")
    manager.verify_worktree(identity)
    registry = AcceptanceSpecRegistry(authority)
    spec, digest = registry.load_and_verify_spec(task_spec=task, expected_policy_hash=gate.policy_hash)
    with authority.store.open_connection() as conn:
        tag = conn.execute("SELECT tag FROM acceptance_seals WHERE digest=?", (digest,)).fetchone()
    if not tag or not hmac.compare_digest(tag[0], spec_seal(gate.controller_key, digest, task)):
        raise VerificationFailedError("Untrusted acceptance specification.")
    command = spec.criteria.get("command", "")
    if (set(spec.criteria) not in ({"command", "min_pass"}, {"command", "min_pass", "worker_script"}) or type(spec.criteria["min_pass"]) is not int
            or spec.criteria["min_pass"] != 1 or not isinstance(command, str) or not command.startswith(("verify-file ", "verify-python "))):
        raise VerificationFailedError("Unsupported native acceptance criteria.")
    method, relative = command.split(" ", 1)
    if method == "verify-python" and ("worker_script" not in spec.criteria or relative == spec.criteria["worker_script"]):
        raise VerificationFailedError("Independent verifier and worker scripts must differ.")
    wt = Path(identity.worktree_path)
    candidate = checked_path(wt, relative)
    git = capture_trusted_git_state(wt, base_sha=task.base_sha, evidence_store=gate.cas, claimed_clean=True)
    reference = f"{task.base_sha}:{relative}"
    if manager._run_git("cat-file", "-t", reference).stdout.strip() != b"blob":
        raise VerificationFailedError("Acceptance path is not a Git blob.")
    size = int(manager._run_git("cat-file", "-s", reference).stdout)
    if size > gate.cas.max_object_bytes:
        raise VerificationFailedError("Candidate exceeds evidence limit.")
    expected = hashlib.sha256(manager._run_git("cat-file", "blob", reference).stdout).hexdigest()
    with os.fdopen(os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
            raise VerificationFailedError("Candidate file identity or size changed.")
        data = fh.read(size + 1)
    if hashlib.sha256(data).hexdigest() != expected:
        raise VerificationFailedError("Candidate differs from the pinned Git blob.")
    # Pass a controller-owned copy to the privileged helper. Workspace paths never cross sudo.
    from opencognitiveos import execution
    script = Path(execution.__file__).with_name("verify_linux_isolation.sh")
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    worker_log = None
    if "worker_script" in spec.criteria:
        with authority.store.open_connection() as conn:
            rows = conn.execute("""SELECT evidence_hash FROM effects WHERE task_unique_key=? AND execution_id=?
                AND epoch=? AND tool='sandbox.python' AND approval_ref=? AND outcome='SUCCEEDED'""",
                (task_key,execution_id,epoch,digest)).fetchall()
        if len(rows) != 1 or not gate.cas.verify_digest(rows[0][0]):
            raise VerificationFailedError("Missing successful worker evidence.")
        worker_log = rows[0][0]
    if method == "verify-python":
        from opencognitiveos.verification.python_check import check_output
        report = check_output(data, gate.cas.read_bytes(worker_log), gate.cas)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "candidate"
            copy.write_bytes(data)
            report = subprocess.check_output(["/bin/bash", str(script), str(copy), expected], env=env,
                                             stderr=subprocess.STDOUT, timeout=45)
        if ("verified candidate blob " + expected).encode() not in report:
            raise VerificationFailedError("Native verifier did not attest the pinned blob.")
    if manager.authorize_workspace(workspace_id, **fence) != identity:
        raise VerificationFailedError("Workspace dispatch changed.")
    manager.verify_worktree(identity)
    if capture_trusted_git_state(wt, base_sha=task.base_sha, evidence_store=gate.cas, claimed_clean=True) != git:
        raise VerificationFailedError("Tested tree changed during native verification.")
    version = subprocess.check_output(["/usr/bin/bwrap", "--version"], env=env, text=True).strip()
    record = ExecutionEnvironmentRecord.capture(repo_root=wt, sandbox_backend="linux-bwrap",
        sandbox_version=version, test_command=command, exit_code=0, passed_count=1, failed_count=0, skipped_count=0)
    log = gate.cas.store_bytes(report, kind=EvidenceKind.EXECUTION_LOG)
    artifacts = {"test_log": log.digest}
    if worker_log:
        artifacts["worker_log"] = worker_log
    verifier = object()
    manifests = EvidenceManifestRegistry(authority=authority, acceptance_registry=registry,
        evidence_store=gate.cas, verifier_capability=verifier, signing_key=gate.verifier_key)
    return manifests.create_and_persist_manifest(task_spec=task, execution_id=execution_id, worker_id=worker_id,
        fencing_epoch=epoch, workspace_id=workspace_id, git_state=git, environment=record,
        artifact_hashes=artifacts, verifier_id=spec.verifier_id, capability=verifier)
