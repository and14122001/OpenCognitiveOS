"""Independent Security & Real OS Sandbox Tests for M2 S22-S25.

Clearly distinguishes:
- Real OS Sandbox tests (Linux unshare user+net+mount+pid namespaces)
- Mock / Unit tests (Fail-closed selection, Seatbelt SBPL profile, Windows AppContainer Fail-Closed)
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencognitiveos.contracts.models import (
    InjectableClock,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.errors import LeaseExpiredError, StaleEpochError
from opencognitiveos.execution.sandbox import (
    GovernedSandboxRunner,
    LinuxUnshareSandboxBackend,
    MacOSSeatbeltSandboxBackend,
    SandboxExecutionRequest,
    SandboxPolicyError,
    SandboxUnavailableError,
    WindowsAppContainerJobBackend,
    select_sandbox_backend,
)
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.storage.sqlite_store import SQLiteStore


class TestM2SandboxS22S25(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)
        self.ws = self.base / "workspace"
        self.ws.mkdir()
        (self.ws / "seed.txt").write_text("workspace-data\n", encoding="utf-8")
        self.host_secret_dir = self.base / "host_secrets"
        self.host_secret_dir.mkdir()
        self.host_secret_file = self.host_secret_dir / "api_key.txt"
        self.host_secret_file.write_text("SECRET_HOST_FILE\n", encoding="utf-8")

        self.db_path = self.base / "ocos.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)

        spec = TaskSpec(
            project_id="proj-s22",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-sandbox",
            goal="sandbox isolation test",
            acceptance_hash=canonical_json_hash({"task": "t-sandbox"}),
        )
        rec, _ = self.auth.submit_task(spec)
        rec = self.auth.transition_task(rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller")
        rec = self.auth.transition_task(rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller")
        self.task_key = rec.unique_key
        self.lease = self.auth.acquire_lease(self.task_key, worker_id="worker-s22", ttl_seconds=60)

        self.linux_backend = LinuxUnshareSandboxBackend()
        self.linux_caps = self.linux_backend.detect_capabilities()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _require_real_linux_os_sandbox(self) -> None:
        if platform.system() != "Linux" or not self.linux_caps.available:
            self.skipTest(f"Real Linux unshare OS sandbox unavailable: {self.linux_caps.reason}")

    def test_real_os_allowed_workspace_rw_and_unauthorized_fs_denied(self) -> None:
        """Real OS Sandbox (S22): Workspace RW succeeds; host secret & /usr write denied."""
        self._require_real_linux_os_sandbox()
        runner = GovernedSandboxRunner(authority=self.auth, backend=self.linux_backend)
        py_code = (
            "from pathlib import Path\n"
            f"ws = Path({str(self.ws)!r})\n"
            "assert (ws / 'seed.txt').read_text().strip() == 'workspace-data'\n"
            "(ws / 'output.txt').write_text('written-inside-sandbox', encoding='utf-8')\n"
            f"secret_exists = Path({str(self.host_secret_file)!r}).exists()\n"
            "usr_blocked = False\n"
            "try:\n"
            "    Path('/usr/ocos_forbidden_write').write_text('x')\n"
            "except OSError:\n"
            "    usr_blocked = True\n"
            "print(f'SECRET={secret_exists} USR_BLOCKED={usr_blocked}')\n"
        )
        res = runner.run(
            SandboxExecutionRequest(
                task_unique_key=self.task_key,
                execution_id=self.lease.execution_id,
                worker_id="worker-s22",
                fencing_epoch=self.lease.fencing_epoch,
                workspace_path=self.ws,
                command=(sys.executable, "-c", py_code),
                timeout_seconds=10.0,
            )
        )
        self.assertTrue(res.is_os_enforced)
        self.assertEqual(res.exit_code, 0, msg=res.stderr)
        self.assertIn("SECRET=False USR_BLOCKED=True", res.stdout)
        self.assertEqual((self.ws / "output.txt").read_text(encoding="utf-8"), "written-inside-sandbox")

    def test_real_os_network_denied_by_default(self) -> None:
        """Real OS Sandbox (S23): Outbound TCP network connection denied in netns."""
        self._require_real_linux_os_sandbox()
        runner = GovernedSandboxRunner(authority=self.auth, backend=self.linux_backend)
        py_code = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(1.0)\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 80))\n"
            "    print('NET_ALLOWED')\n"
            "except OSError as e:\n"
            "    print(f'NET_DENIED:{e.errno}')\n"
        )
        res = runner.run(
            SandboxExecutionRequest(
                task_unique_key=self.task_key,
                execution_id=self.lease.execution_id,
                worker_id="worker-s22",
                fencing_epoch=self.lease.fencing_epoch,
                workspace_path=self.ws,
                command=(sys.executable, "-c", py_code),
                timeout_seconds=10.0,
            )
        )
        self.assertEqual(res.exit_code, 0, msg=res.stderr)
        self.assertIn("NET_DENIED:", res.stdout)
        self.assertNotIn("NET_ALLOWED", res.stdout)

        with self.assertRaises(SandboxPolicyError):
            runner.run(
                SandboxExecutionRequest(
                    task_unique_key=self.task_key,
                    execution_id=self.lease.execution_id,
                    worker_id="worker-s22",
                    fencing_epoch=self.lease.fencing_epoch,
                    workspace_path=self.ws,
                    command=(sys.executable, "-c", "print('hi')"),
                    network_mode="ALLOW",
                )
            )

    def test_real_os_env_whitelist_blocks_parent_and_child_credentials(self) -> None:
        """Real OS Sandbox (S24): Neither sandboxed parent nor grandchild can read host credentials."""
        self._require_real_linux_os_sandbox()
        runner = GovernedSandboxRunner(authority=self.auth, backend=self.linux_backend)
        py_code = (
            "import os, subprocess, sys\n"
            "parent_leaks = [k for k in ('OPENAI_API_KEY','GITHUB_TOKEN','GEMINI_API_KEY','AWS_SECRET_ACCESS_KEY') if k in os.environ]\n"
            "child = subprocess.run([sys.executable, '-c', \"import os; print([k for k in ('OPENAI_API_KEY','GITHUB_TOKEN','GEMINI_API_KEY','AWS_SECRET_ACCESS_KEY') if k in os.environ])\"], capture_output=True, text=True, check=True)\n"
            "print(f'PARENT={parent_leaks} CHILD={child.stdout.strip()} ALLOWED={os.environ.get(\"OCOS_SAFE_FLAG\")}')\n"
        )
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-secret",
                "GITHUB_TOKEN": "ghp_secret",
                "GEMINI_API_KEY": "AIzaSecret",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
                "OCOS_SAFE_FLAG": "yes",
            },
            clear=False,
        ):
            res = runner.run(
                SandboxExecutionRequest(
                    task_unique_key=self.task_key,
                    execution_id=self.lease.execution_id,
                    worker_id="worker-s22",
                    fencing_epoch=self.lease.fencing_epoch,
                    workspace_path=self.ws,
                    command=(sys.executable, "-c", py_code),
                    env_whitelist=("OCOS_SAFE_FLAG",),
                    timeout_seconds=10.0,
                )
            )
        self.assertEqual(res.exit_code, 0, msg=res.stderr)
        self.assertIn("PARENT=[] CHILD=[] ALLOWED=yes", res.stdout)

    def test_real_os_timeout_kills_entire_process_group(self) -> None:
        """Real OS Sandbox (S25): Timeout kills parent + spawned child process group."""
        self._require_real_linux_os_sandbox()
        runner = GovernedSandboxRunner(authority=self.auth, backend=self.linux_backend)
        py_code = (
            "import subprocess, time\n"
            "subprocess.Popen(['sleep', '60'])\n"
            "time.sleep(60)\n"
        )
        res = runner.run(
            SandboxExecutionRequest(
                task_unique_key=self.task_key,
                execution_id=self.lease.execution_id,
                worker_id="worker-s22",
                fencing_epoch=self.lease.fencing_epoch,
                workspace_path=self.ws,
                command=(sys.executable, "-c", py_code),
                timeout_seconds=0.4,
            )
        )
        self.assertTrue(res.timed_out)
        self.assertTrue(res.process_group_terminated)
        self.assertFalse(self.linux_backend._is_pgid_alive(res.pgid))

    def test_real_os_worker_crash_preserves_evidence(self) -> None:
        """Real OS Sandbox (S25): Worker crash preserves stdout, stderr, exit_code, and evidence_hash."""
        self._require_real_linux_os_sandbox()
        runner = GovernedSandboxRunner(authority=self.auth, backend=self.linux_backend)
        py_code = (
            "import sys\n"
            "print('partial-stdout')\n"
            "print('fatal-stderr', file=sys.stderr)\n"
            "sys.exit(42)\n"
        )
        res = runner.run(
            SandboxExecutionRequest(
                task_unique_key=self.task_key,
                execution_id=self.lease.execution_id,
                worker_id="worker-s22",
                fencing_epoch=self.lease.fencing_epoch,
                workspace_path=self.ws,
                command=(sys.executable, "-c", py_code),
                timeout_seconds=10.0,
            )
        )
        self.assertEqual(res.exit_code, 42)
        self.assertFalse(res.timed_out)
        self.assertIn("partial-stdout", res.stdout)
        self.assertIn("fatal-stderr", res.stderr)
        self.assertEqual(len(res.evidence_hash), 64)

    def test_fail_closed_when_sandbox_unavailable(self) -> None:
        """Unit/Mock: When no OS sandbox is available, refuse execution (never fall back)."""
        unavailable = LinuxUnshareSandboxBackend(unshare_bin="/nonexistent/unshare")
        with self.assertRaises(SandboxUnavailableError):
            select_sandbox_backend(candidates=(unavailable,))
        with self.assertRaises(SandboxUnavailableError):
            select_sandbox_backend(candidates=(WindowsAppContainerJobBackend(),))

    def test_stale_lease_and_wrong_epoch_rejected_before_execution(self) -> None:
        """Governance: Expired lease or wrong fencing_epoch cannot execute in sandbox."""
        backend = self.linux_backend if self.linux_caps.available else MacOSSeatbeltSandboxBackend()
        runner = GovernedSandboxRunner(authority=self.auth, backend=backend)

        # Wrong fencing_epoch
        with self.assertRaises(StaleEpochError):
            runner.run(
                SandboxExecutionRequest(
                    task_unique_key=self.task_key,
                    execution_id=self.lease.execution_id,
                    worker_id="worker-s22",
                    fencing_epoch=self.lease.fencing_epoch + 1,
                    workspace_path=self.ws,
                    command=(sys.executable, "-c", "print('should not run')"),
                )
            )

        # Expired lease
        self.clock.advance(seconds=120)
        with self.assertRaises(LeaseExpiredError):
            runner.run(
                SandboxExecutionRequest(
                    task_unique_key=self.task_key,
                    execution_id=self.lease.execution_id,
                    worker_id="worker-s22",
                    fencing_epoch=self.lease.fencing_epoch,
                    workspace_path=self.ws,
                    command=(sys.executable, "-c", "print('should not run')"),
                )
            )

    def test_macos_seatbelt_profile_generation_unit(self) -> None:
        """Unit/Mock: Verify macOS Seatbelt SBPL profile denies default & network and scopes workspace."""
        # Use the target platform's path even on Windows CI.
        with mock.patch.object(Path, "resolve", return_value="/private/tmp/ocos-work"):
            sbpl = MacOSSeatbeltSandboxBackend.build_sbpl_profile(self.ws)
        self.assertIn("(deny default)", sbpl)
        self.assertIn("(deny network*)", sbpl)
        self.assertIn('(subpath "/private/tmp/ocos-work")', sbpl)


if __name__ == "__main__":
    unittest.main()
