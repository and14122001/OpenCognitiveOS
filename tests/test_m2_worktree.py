"""Tests for M2 S21-S22 Fixed-SHA Git Worktree & SQLite-Governed Workspace Lifecycle (Round 2)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from opencognitiveos.contracts.models import (
    InjectableClock,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.execution.worktree import (
    WorkspaceConflictError,
    WorkspaceIdentityError,
    WorkspaceManager,
    WorktreeVerificationError,
)
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.storage.sqlite_store import SQLiteStore


def _init_git_repo(repo_dir: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@local"], check=True, capture_output=True)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)
    res = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return res.stdout.strip()


class TestM2WorktreeS21S22Round2(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.repo_dir = base / "repo"
        self.repo_dir.mkdir()
        self.base_sha = _init_git_repo(self.repo_dir)
        self.workspaces_root = base / "workspaces"
        self.db_path = base / "ocos.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)
        self.mgr = WorkspaceManager(
            authority=self.auth,
            repo_path=self.repo_dir,
            workspaces_root=self.workspaces_root,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _register_task(self, task_id: str, *, with_lease: bool = False, repo_id: str = "and14122001/OpenCognitiveOS") -> str:
        spec = TaskSpec(
            project_id="proj-m2",
            repo_id=repo_id,
            task_id=task_id,
            goal=f"goal for {task_id}",
            base_sha=self.base_sha,
            acceptance_hash=canonical_json_hash({"task_id": task_id}),
        )
        rec, _ = self.auth.submit_task(spec)
        rec = self.auth.transition_task(rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability)
        rec = self.auth.transition_task(rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability)
        if with_lease:
            self.auth.acquire_lease(rec.unique_key, worker_id=f"worker-{task_id}", ttl_seconds=60)
        return rec.unique_key

    def test_creation_claim_blocks_new_lease(self):
        from opencognitiveos.kernel.authority import ReconciliationRequiredError
        key = self._register_task("creating-claim")
        def probe(path):
            self.assertEqual(self.mgr.load_identity("creating-claim").status, "CREATING")
            with self.assertRaises(ReconciliationRequiredError):
                self.auth.acquire_lease(key, worker_id="racing-worker")
        ident = self.mgr.create_workspace(workspace_id="creating-claim", repo_id="and14122001/OpenCognitiveOS", task_id="creating-claim", base_sha=self.base_sha, before_finalize_hook=probe)
        self.assertEqual(ident.status, "ACTIVE")
        self.auth.acquire_lease(key, worker_id="after-create")

    def test_disjoint_workspace_roots(self):
        for root in (self.repo_dir, self.repo_dir / "nested", self.repo_dir.parent):
            with self.subTest(root=root), self.assertRaises(WorktreeVerificationError):
                WorkspaceManager(authority=self.auth, repo_path=self.repo_dir, workspaces_root=root)

    def test_nested_workspace_claim_rejected(self):
        self._register_task("nested")
        args = dict(repo_id="and14122001/OpenCognitiveOS", task_id="nested", base_sha=self.base_sha)
        self.mgr.create_workspace(workspace_id="outer", relative_dir="outer", **args)
        with self.assertRaises(WorkspaceConflictError):
            self.mgr.create_workspace(workspace_id="inner", relative_dir="outer/inner", **args)

    def test_cleanup_claim_blocks_new_lease_and_duplicate_cleanup(self):
        from opencognitiveos.kernel.authority import ReconciliationRequiredError
        key = self._register_task("claim")
        ident = self.mgr.create_workspace(workspace_id="claim", repo_id="and14122001/OpenCognitiveOS", task_id="claim", base_sha=self.base_sha)
        args = dict(expected_repo_id=ident.repo_id, expected_task_id=ident.task_id, expected_base_sha=self.base_sha)
        original = self.mgr.verify_worktree
        def probe(identity):
            self.assertEqual(self.mgr.load_identity("claim").status, "CLEANING")
            with self.assertRaises(ReconciliationRequiredError):
                self.auth.acquire_lease(key, "racing-worker")
            with self.assertRaises(WorkspaceIdentityError):
                self.mgr.cleanup_workspace("claim", **args)
            self.assertIn("claim", self.mgr.recover_workspaces().needs_reconciliation)
            return original(identity)
        with mock.patch.object(self.mgr, "verify_worktree", side_effect=probe):
            self.assertEqual(self.mgr.cleanup_workspace("claim", **args).status, "CLEANED")

    def test_expired_lease_is_not_quiescence(self):
        key = self._register_task("expired-cleanup")
        ident = self.mgr.create_workspace(workspace_id="expired-cleanup", repo_id="and14122001/OpenCognitiveOS", task_id="expired-cleanup", base_sha=self.base_sha)
        self.auth.acquire_lease(key, worker_id="worker")
        self.clock.advance(seconds=120)
        with self.assertRaises(WorkspaceIdentityError):
            self.mgr.cleanup_workspace(ident.workspace_id, expected_repo_id=ident.repo_id, expected_task_id=ident.task_id, expected_base_sha=self.base_sha)
        self.assertTrue(Path(ident.worktree_path).exists())

    def test_task_spec_sha_mismatch(self):
        self._register_task("sha-bound")
        subprocess.run(["git", "-C", str(self.repo_dir), "commit", "--allow-empty", "-m", "other"], check=True, capture_output=True)
        other = subprocess.check_output(["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaises(WorkspaceIdentityError):
            self.mgr.create_workspace(workspace_id="sha-bound", repo_id="and14122001/OpenCognitiveOS", task_id="sha-bound", base_sha=other)
        self.assertFalse((self.workspaces_root / "sha-bound").exists())

    def test_portable_case_alias(self):
        self._register_task("alias")
        self.mgr.create_workspace(workspace_id="alias1", repo_id="and14122001/OpenCognitiveOS", task_id="alias", base_sha=self.base_sha, relative_dir="Case")
        with self.assertRaises(WorkspaceConflictError):
            self.mgr.create_workspace(workspace_id="alias2", repo_id="and14122001/OpenCognitiveOS", task_id="alias", base_sha=self.base_sha, relative_dir="case")

    def test_create_verify_and_cleanup_fixed_sha_worktree(self) -> None:
        self._register_task("t-s21")
        ident = self.mgr.create_workspace(
            workspace_id="ws-001",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-s21",
            base_sha=self.base_sha,
        )
        self.assertEqual(ident.status, "ACTIVE")
        self.assertEqual(ident.base_sha, self.base_sha)
        self.assertEqual(ident.lock_reason, "opencognitiveos:ws-001:t-s21")
        self.assertIsNotNone(ident.git_admin_dir)

        wt_path = Path(ident.worktree_path)
        self.assertTrue((wt_path / "README.md").is_file())
        self.assertTrue((wt_path / ".git").is_file())

        entry = self.mgr.verify_worktree(ident)
        self.assertEqual(entry["HEAD"], self.base_sha)
        self.assertTrue(entry.get("detached"))
        self.assertTrue(entry.get("locked"))
        self.assertEqual(entry.get("lock_reason"), "opencognitiveos:ws-001:t-s21")

        cleaned = self.mgr.cleanup_workspace(
            "ws-001",
            expected_repo_id="and14122001/OpenCognitiveOS",
            expected_task_id="t-s21",
            expected_base_sha=self.base_sha,
        )
        self.assertEqual(cleaned.status, "CLEANED")
        self.assertFalse(wt_path.exists())

    def test_reject_floating_refs_short_sha_and_nonexistent_sha(self) -> None:
        self._register_task("t-bad-sha")
        for bad_sha in ("HEAD", "main", "refs/heads/main", self.base_sha[:12], "0" * 40, ""):
            with self.subTest(bad_sha=bad_sha), self.assertRaises(WorktreeVerificationError):
                self.mgr.create_workspace(
                    workspace_id="ws-bad-sha",
                    repo_id="and14122001/OpenCognitiveOS",
                    task_id="t-bad-sha",
                    base_sha=bad_sha,
                )

    def test_reject_path_traversal_and_symlink_in_workspace_path(self) -> None:
        self._register_task("t-path")
        with self.assertRaises(ValueError):
            self.mgr.create_workspace(
                workspace_id="ws-trav",
                repo_id="and14122001/OpenCognitiveOS",
                task_id="t-path",
                base_sha=self.base_sha,
                relative_dir="../escape",
            )
        outside = Path(self.tmpdir.name) / "outside"
        outside.mkdir()
        try:
            (self.workspaces_root / "sym").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(ValueError):
            self.mgr.create_workspace(
                workspace_id="ws-sym",
                repo_id="and14122001/OpenCognitiveOS",
                task_id="t-path",
                base_sha=self.base_sha,
                relative_dir="sym/sub",
            )

    def test_controlled_git_subprocess_rejects_external_config_hooks_and_enforces_timeout(self) -> None:
        with self.assertRaises(WorktreeVerificationError):
            WorkspaceManager(
                authority=self.auth,
                repo_path=self.repo_dir,
                workspaces_root=self.workspaces_root,
                git_executable="git",  # relative executable must be rejected
            )

        self._register_task("t-env")
        # Inject hostile GIT_* env vars; controlled env must isolate subprocess
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": "/nonexistent/evil.git",
                "GIT_WORK_TREE": "/nonexistent/evil",
                "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/evil'",
                "GIT_CONFIG_COUNT": "99",
            },
            clear=False,
        ):
            ident = self.mgr.create_workspace(
                workspace_id="ws-env",
                repo_id="and14122001/OpenCognitiveOS",
                task_id="t-env",
                base_sha=self.base_sha,
            )
            self.assertEqual(ident.status, "ACTIVE")

        # Timeout enforcement
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=0.01)):
            with self.assertRaises(WorktreeVerificationError):
                self.mgr.list_git_worktrees()

    def test_different_ids_same_canonical_path_race_prevented(self) -> None:
        """Fault Injection #1: Two different workspace_ids competing for the exact same canonical path."""
        self._register_task("t-race-1")
        self._register_task("t-race-2")

        barrier = threading.Barrier(2)
        successes: list[str] = []
        conflicts: list[Exception] = []
        lock = threading.Lock()

        def try_create(ws_id: str, task_id: str) -> None:
            local_auth = ExecutionAuthority(SQLiteStore(self.db_path, clock=self.clock), clock=self.clock)
            local_mgr = WorkspaceManager(
                authority=local_auth,
                repo_path=self.repo_dir,
                workspaces_root=self.workspaces_root,
            )
            barrier.wait()
            try:
                ident = local_mgr.create_workspace(
                    workspace_id=ws_id,
                    repo_id="and14122001/OpenCognitiveOS",
                    task_id=task_id,
                    base_sha=self.base_sha,
                    relative_dir="shared-canonical-dir",
                )
                with lock:
                    successes.append(ident.workspace_id)
            except WorkspaceConflictError as exc:
                with lock:
                    conflicts.append(exc)

        t1 = threading.Thread(target=try_create, args=("ws-alpha", "t-race-1"))
        t2 = threading.Thread(target=try_create, args=("ws-beta", "t-race-2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

    def test_create_and_recovery_race_respects_active_lease(self):
        key = self._register_task("t-leased")
        ident = self.mgr.create_workspace(workspace_id="ws-leased", repo_id="and14122001/OpenCognitiveOS", task_id="t-leased", base_sha=self.base_sha)
        self.auth.acquire_lease(key, worker_id="worker")
        self.assertIn(ident.workspace_id, self.mgr.recover_workspaces().skipped_active_lease)
        self.clock.advance(seconds=1200)
        self.assertIn(ident.workspace_id, self.mgr.recover_workspaces().needs_reconciliation)
        self.assertTrue(Path(ident.worktree_path).exists())

    def test_provision_rejects_existing_live_and_expired_lease(self):
        self._register_task("leased-before-create", with_lease=True)
        for delay in (0, 120):
            self.clock.advance(seconds=delay)
            with self.assertRaises(WorkspaceIdentityError):
                self.mgr.create_workspace(workspace_id="blocked", repo_id="and14122001/OpenCognitiveOS", task_id="leased-before-create", base_sha=self.base_sha)
            self.assertFalse((self.workspaces_root / "blocked").exists())

    def test_forged_git_pointer_and_bidirectional_metadata_rejected_to_needs_reconciliation(self) -> None:
        """Fault Injection #3: Forged .git pointer or tampered admin metadata -> NEEDS_RECONCILIATION, no delete."""
        self._register_task("t-forged")
        ident = self.mgr.create_workspace(
            workspace_id="ws-forged-ptr",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-forged",
            base_sha=self.base_sha,
        )
        wt_path = Path(ident.worktree_path)
        git_ptr = wt_path / ".git"
        original_ptr = git_ptr.read_text(encoding="utf-8")

        # Tamper .git pointer to forged location
        with git_ptr.open("r+", encoding="utf-8") as pointer:
            pointer.write("gitdir: /tmp/forged-evil-repo/.git/worktrees/x\n")
            pointer.truncate()
        with self.assertRaises(WorktreeVerificationError):
            self.mgr.cleanup_workspace(
                "ws-forged-ptr",
                expected_repo_id="and14122001/OpenCognitiveOS",
                expected_task_id="t-forged",
                expected_base_sha=self.base_sha,
            )
        self.assertTrue(wt_path.is_dir())
        self.assertEqual(self.mgr.load_identity("ws-forged-ptr").status, "NEEDS_RECONCILIATION")

        # Also test bidirectional admin locked reason tampering on a fresh workspace
        self._register_task("t-forged-lock")
        ident2 = self.mgr.create_workspace(
            workspace_id="ws-forged-lock",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-forged-lock",
            base_sha=self.base_sha,
        )
        admin_dir = Path(ident2.git_admin_dir)  # type: ignore[arg-type]
        (admin_dir / "locked").write_text("other-attacker-reason\n", encoding="utf-8")
        with self.assertRaises(WorktreeVerificationError):
            self.mgr.cleanup_workspace(
                "ws-forged-lock",
                expected_repo_id="and14122001/OpenCognitiveOS",
                expected_task_id="t-forged-lock",
                expected_base_sha=self.base_sha,
            )
        self.assertTrue(Path(ident2.worktree_path).is_dir())
        self.assertEqual(self.mgr.load_identity("ws-forged-lock").status, "NEEDS_RECONCILIATION")

        # Restore original_ptr on ws-forged-ptr just so tmpdir cleanup has no permission issues
        with git_ptr.open("r+", encoding="utf-8") as pointer:
            pointer.write(original_ptr)
            pointer.truncate()

    def test_git_worktree_remove_failure_enters_needs_reconciliation_without_rmtree(self) -> None:
        """Fault Injection #4: Git remove failure -> NEEDS_RECONCILIATION, never rmtree or prune."""
        self._register_task("t-rem-fail")
        ident = self.mgr.create_workspace(
            workspace_id="ws-rem-fail",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-rem-fail",
            base_sha=self.base_sha,
        )
        wt_path = Path(ident.worktree_path)

        orig_run_git = self.mgr._run_git

        def failing_remove(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
            if len(args) >= 2 and args[0] == "worktree" and args[1] == "remove":
                return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout=b"", stderr=b"mock remove error")
            return orig_run_git(*args, check=check)

        with mock.patch("shutil.rmtree") as mock_rmtree, mock.patch.object(
            self.mgr, "_run_git", side_effect=failing_remove
        ) as mock_git:
            with self.assertRaises(WorktreeVerificationError):
                self.mgr.cleanup_workspace(
                    "ws-rem-fail",
                    expected_repo_id="and14122001/OpenCognitiveOS",
                    expected_task_id="t-rem-fail",
                    expected_base_sha=self.base_sha,
                )
            mock_rmtree.assert_not_called()
            for call in mock_git.call_args_list:
                self.assertNotIn("prune", call.args)

        self.assertTrue(wt_path.is_dir())
        self.assertEqual(self.mgr.load_identity("ws-rem-fail").status, "NEEDS_RECONCILIATION")

    def test_identity_write_interruption_rolls_back_atomically(self) -> None:
        """Fault Injection #5: DB commit interruption during CREATING -> ACTIVE rolls back atomically."""
        self._register_task("t-db-int")

        def abort_commit() -> None:
            raise OSError("Simulated disk/commit interruption")

        with self.assertRaises(OSError):
            self.mgr.create_workspace(
                workspace_id="ws-db-int",
                repo_id="and14122001/OpenCognitiveOS",
                task_id="t-db-int",
                base_sha=self.base_sha,
                before_commit_hook=abort_commit,
            )

        # Status must remain atomically CREATING (not half-written or corrupt)
        ident = self.mgr.load_identity("ws-db-int")
        self.assertEqual(ident.status, "CREATING")

        # Recovery preserves interrupted creation even without a lease.
        report = self.mgr.recover_workspaces()
        self.assertEqual(report.cleaned_orphans, ())
        self.assertIn("ws-db-int", report.needs_reconciliation)
        self.assertEqual(self.mgr.load_identity("ws-db-int").status, "CREATING")

    def test_unknown_or_unregistered_directory_preserved_never_rmtree(self) -> None:
        """Fault Injection #6: Unknown / unregistered directories are preserved; never rmtree or prune."""
        unknown_dir = self.workspaces_root / "unknown-foreign-dir"
        unknown_dir.mkdir(parents=True)
        (unknown_dir / "precious.txt").write_text("do not delete\n", encoding="utf-8")

        # Also simulate a CREATING record whose target_path was replaced by an unregistered dir
        self._register_task("t-unreg")

        def replace_with_unregistered_dir(wt_path: Path) -> None:
            # Simulate an unverified unregistered dir state by removing .git pointer file
            (wt_path / ".git").unlink()
            raise RuntimeError("Crash leaving unregistered dir")

        with self.assertRaises(RuntimeError):
            self.mgr.create_workspace(
                workspace_id="ws-unreg",
                repo_id="and14122001/OpenCognitiveOS",
                task_id="t-unreg",
                base_sha=self.base_sha,
                before_finalize_hook=replace_with_unregistered_dir,
            )

        with mock.patch("shutil.rmtree") as mock_rmtree:
            report = self.mgr.recover_workspaces()
            mock_rmtree.assert_not_called()

        self.assertTrue((unknown_dir / "precious.txt").is_file())
        self.assertTrue((self.workspaces_root / "ws-unreg").is_dir())
        self.assertIn("ws-unreg", report.needs_reconciliation)
        self.assertEqual(self.mgr.load_identity("ws-unreg").status, "CREATING")

    def test_create_subprocess_exception_preserves_partial_workspace(self):
        key = self._register_task("create-exception")
        original = self.mgr._run_git
        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ("worktree", "add"):
                raise OSError("injected failure after git add")
            return result
        with mock.patch.object(self.mgr, "_run_git", side_effect=fail), self.assertRaises(OSError):
            self.mgr.create_workspace(workspace_id="create-exception", repo_id="and14122001/OpenCognitiveOS", task_id="create-exception", base_sha=self.base_sha)
        ident = self.mgr.load_identity("create-exception")
        self.assertEqual(ident.status, "NEEDS_RECONCILIATION")
        self.assertTrue(Path(ident.worktree_path).is_dir())
        self.assertIn(ident.workspace_id, self.mgr.recover_workspaces().needs_reconciliation)
        from opencognitiveos.errors import ReconciliationRequiredError
        with self.assertRaises(ReconciliationRequiredError):
            self.auth.acquire_lease(key, "worker")

    def test_cleanup_subprocess_exception_preserves_workspace(self):
        self._register_task("cleanup-exception")
        args = dict(repo_id="and14122001/OpenCognitiveOS", task_id="cleanup-exception", base_sha=self.base_sha)
        ident = self.mgr.create_workspace(workspace_id="cleanup-exception", **args)
        original = self.mgr._run_git
        def fail(*args, **kwargs):
            if args[:2] == ("worktree", "unlock"):
                raise OSError("injected subprocess failure")
            return original(*args, **kwargs)
        with mock.patch.object(self.mgr, "_run_git", side_effect=fail), self.assertRaises(OSError):
            self.mgr.cleanup_workspace(ident.workspace_id, expected_repo_id=ident.repo_id, expected_task_id=ident.task_id, expected_base_sha=ident.base_sha)
        self.assertTrue(Path(ident.worktree_path).exists())
        self.assertEqual(self.mgr.load_identity(ident.workspace_id).status, "NEEDS_RECONCILIATION")

    def test_dirty_workspace_preserved(self):
        self._register_task("dirty")
        ident = self.mgr.create_workspace(workspace_id="dirty", repo_id="and14122001/OpenCognitiveOS", task_id="dirty", base_sha=self.base_sha)
        precious = Path(ident.worktree_path) / "precious.txt"
        precious.write_text("preserve", encoding="utf-8")
        with self.assertRaises(WorktreeVerificationError):
            self.mgr.cleanup_workspace("dirty", expected_repo_id=ident.repo_id, expected_task_id=ident.task_id, expected_base_sha=self.base_sha)
        self.assertEqual(precious.read_text(), "preserve")
        self.assertEqual(self.mgr.load_identity("dirty").status, "NEEDS_RECONCILIATION")

    def test_workspace_dispatch_fences_stale_worker(self):
        key = self._register_task("dispatch")
        self.mgr.create_workspace(workspace_id="dispatch", repo_id="and14122001/OpenCognitiveOS", task_id="dispatch", base_sha=self.base_sha)
        lease = self.auth.acquire_lease(key, "worker", ttl_seconds=60)
        args = dict(worker_id="worker", execution_id=lease.execution_id, epoch=lease.fencing_epoch)
        self.assertEqual(self.mgr.authorize_workspace("dispatch", **args).workspace_id, "dispatch")
        with self.assertRaises(WorkspaceIdentityError):
            self.mgr.authorize_workspace("dispatch", **dict(args, execution_id="wrong"))
        self.clock.advance(seconds=61)
        from opencognitiveos.errors import LeaseExpiredError, ReconciliationRequiredError
        with self.assertRaises(LeaseExpiredError):
            self.mgr.authorize_workspace("dispatch", **args)
        with self.assertRaises(ReconciliationRequiredError):
            self.auth.acquire_lease(key, "replacement")

    def test_sandbox_workspace_binding_gate(self):
        from dataclasses import replace
        from opencognitiveos.execution.sandbox import (GovernedSandboxRunner, SandboxExecutionRequest, SandboxPolicyError, SandboxUnavailableError)
        key = self._register_task("sandbox-bound")
        ident = self.mgr.create_workspace(workspace_id="sandbox-bound", repo_id="and14122001/OpenCognitiveOS", task_id="sandbox-bound", base_sha=self.base_sha)
        lease = self.auth.acquire_lease(key, "worker", ttl_seconds=60)
        backend = mock.Mock()
        req = SandboxExecutionRequest(key, lease.execution_id, "worker", lease.fencing_epoch, Path(ident.worktree_path), ("true",), workspace_id=ident.workspace_id)
        runner = GovernedSandboxRunner(authority=self.auth, backend=backend, workspace_manager=self.mgr)
        for invalid in (replace(req, workspace_id=None), replace(req, workspace_path=self.mgr.repo_path)):
            with self.assertRaises(SandboxPolicyError):
                runner.run(invalid)
        with self.assertRaises(SandboxUnavailableError):
            runner.run(req)
        backend.execute.assert_not_called()

    def test_cleanup_refuses_dispatched_workspace_without_lease(self):
        key = self._register_task("dispatched-cleanup")
        ident = self.mgr.create_workspace(workspace_id="dispatched-cleanup", repo_id="and14122001/OpenCognitiveOS", task_id="dispatched-cleanup", base_sha=self.base_sha)
        self.auth.acquire_lease(key, worker_id="worker")
        # Simulate lease removal without proof that the worker has exited.
        with self.mgr.store.transaction() as conn:
            conn.execute("DELETE FROM leases WHERE task_unique_key = ?", (key,))
        with self.assertRaisesRegex(WorkspaceIdentityError, "termination evidence"):
            self.mgr.cleanup_workspace(ident.workspace_id, expected_repo_id=ident.repo_id, expected_task_id=ident.task_id, expected_base_sha=self.base_sha)
        self.assertTrue(Path(ident.worktree_path).is_dir())

    def test_cleanup_refuses_active_lease(self):
        key = self._register_task("live")
        ident = self.mgr.create_workspace(workspace_id="live", repo_id="and14122001/OpenCognitiveOS", task_id="live", base_sha=self.base_sha)
        self.auth.acquire_lease(key, worker_id="worker")
        with self.assertRaises(WorkspaceIdentityError):
            self.mgr.cleanup_workspace("live", expected_repo_id=ident.repo_id, expected_task_id=ident.task_id, expected_base_sha=self.base_sha)
        self.assertTrue(Path(ident.worktree_path).is_dir())
        self.assertEqual(self.mgr.load_identity("live").status, "ACTIVE")

    def test_cross_task_cleanup_refused_without_deleting_valid_workspace(self) -> None:
        """Fault Injection #7: Cross-task cleanup attempt refused without deleting or corrupting Task A's workspace."""
        self._register_task("t-owner-a")
        self._register_task("t-attacker-b")
        ident = self.mgr.create_workspace(
            workspace_id="ws-owner-a",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-owner-a",
            base_sha=self.base_sha,
        )
        wt_path = Path(ident.worktree_path)

        with self.assertRaises(WorkspaceIdentityError):
            self.mgr.cleanup_workspace(
                "ws-owner-a",
                expected_repo_id="and14122001/OpenCognitiveOS",
                expected_task_id="t-attacker-b",
                expected_base_sha=self.base_sha,
            )

        # Owner A's workspace remains ACTIVE and intact
        self.assertTrue(wt_path.is_dir())
        self.assertEqual(self.mgr.load_identity("ws-owner-a").status, "ACTIVE")


if __name__ == "__main__":
    unittest.main()
