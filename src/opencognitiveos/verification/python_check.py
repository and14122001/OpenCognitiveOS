"""Run a pinned verifier against a read-only candidate in a separate bounded native unit."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from opencognitiveos import execution
from opencognitiveos.errors import VerificationFailedError
from opencognitiveos.verification.evidence_store import EvidenceKind


def check_output(test, candidate, cas):
    if len(test) > 1048576 or len(candidate) > 1048576:
        raise VerificationFailedError("Verifier input exceeds limit.")
    unit = f"ocos-verifier-{uuid.uuid4().hex}.service"
    launcher = Path(execution.__file__).with_name("launch_python.sh")
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        script, artifact, namespace, log = (root / n for n in ("test.py", "candidate", "namespace", "result"))
        script.write_bytes(test)
        artifact.write_bytes(candidate)
        namespace.touch(mode=0o600)
        log.touch(mode=0o600)
        cmd = ["/usr/bin/sudo", "-n", "/usr/bin/systemd-run", "--quiet", "--expand-environment=no",
               "--unit="+unit, "--wait", "--collect", "-p", "MemoryMax=128M", "-p", "TasksMax=16",
               "-p", "CPUQuota=50%", "-p", "RuntimeMaxSec=10", "-p", "TimeoutStopSec=1",
               "-p", "KillMode=control-group", "-p", "LimitCORE=0", "-p", "LimitFSIZE=1048576",
               "-p", "StandardOutput=file:"+str(log), "-p", "StandardError=inherit", "/bin/bash",
               str(launcher), str(script), str(namespace), str(artifact)]
        start = time.monotonic()
        completed = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=16)
        elapsed = time.monotonic() - start
        state = subprocess.run(["/usr/bin/systemctl", "show", unit, "--property=ActiveState,SubState"],
                               env=env, text=True, capture_output=True, timeout=5)
        if set(state.stdout.splitlines()) != {"ActiveState=inactive", "SubState=dead"}:
            raise VerificationFailedError("Verifier quiescence unconfirmed.")
        raw = log.read_bytes()
        if (completed.returncode or elapsed >= 10 or not namespace.stat().st_size
                or not json.loads(namespace.read_bytes()).get("child-pid") or len(raw) >= 1048576):
            digest = cas.store_bytes(raw, kind=EvidenceKind.EXECUTION_LOG).digest
            raise VerificationFailedError("Independent Python check failed. Log SHA256 " + digest)
        return raw
