"""S30: Independent Security & Counterexample Regression Tests for S26, S28, S29."""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from opencognitiveos.contracts.models import (
    InjectableClock,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.errors import (
    DatabaseIntegrityError,
    InvalidTransitionError,
    LeaseExpiredError,
    PolicyDeniedError,
    StaleEpochError,
    VerificationFailedError,
)
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification import (
    AcceptanceSpec,
    AcceptanceSpecRegistry,
    ContentAddressedEvidenceStore,
    EvidenceIntegrityError,
    EvidenceKind,
    EvidenceManifestRegistry,
    ExecutionEnvironmentRecord,
    capture_trusted_git_state,
)


def _init_repo(repo_dir: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "R4"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "r4@local"], check=True, capture_output=True)
    (repo_dir / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "app.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


class TestM2S26S28S29S30Security(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)
        self.repo_dir = self.base / "repo"
        self.repo_dir.mkdir()
        self.base_sha = _init_repo(self.repo_dir)

        self.db_path = self.base / "ocos.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)
        self.spec_key = secrets.token_bytes(32)
        self.manifest_key = secrets.token_bytes(32)
        self.controller_capability = object()
        self.acceptance_reg = AcceptanceSpecRegistry(self.auth, clock=self.clock, controller_capability=self.controller_capability, signing_key=self.spec_key)
        self.evidence_store = ContentAddressedEvidenceStore(self.base / "cas", clock=self.clock)
        self.verifier_capability = object()
        self.manifest_reg = EvidenceManifestRegistry(
            authority=self.auth,
            acceptance_registry=self.acceptance_reg,
            verifier_capability=self.verifier_capability,
            signing_key=self.manifest_key,
            evidence_store=self.evidence_store,
            clock=self.clock,
        )
        self.policy_hash = canonical_json_hash({"network": "DENY", "sandbox": "strict"})

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _make_task_and_spec(
        self,
        task_id: str,
        *,
        project_id: str = "proj-r4",
        verifier_id: str = "verifier-trusted-01",
        expires_at_utc: str | None = None,
        start_task: bool = True,
        command: str = "python3 -m unittest",
    ) -> tuple[TaskSpec, AcceptanceSpec, str]:
        task_spec = TaskSpec(
            project_id=project_id,
            repo_id="and14122001/OpenCognitiveOS",
            task_id=task_id,
            base_sha=self.base_sha,
            goal=f"verify {task_id}",
            acceptance_hash=canonical_json_hash({"goal": f"verify {task_id}"}),
        )
        rec, _ = self.auth.submit_task(task_spec)
        rec = self.auth.transition_task(rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability)
        rec = self.auth.transition_task(rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability)

        acc_spec = AcceptanceSpec(
            project_id=project_id,
            repo_id="and14122001/OpenCognitiveOS",
            task_unique_key=task_spec.unique_key,
            base_sha=self.base_sha,
            policy_hash=self.policy_hash,
            verifier_id=verifier_id,
            criteria={"command": command, "min_pass": 1},
            created_at_utc=self.clock.now_iso(),
            expires_at_utc=expires_at_utc,
        )
        spec_hash = self.acceptance_reg.register_spec(acc_spec, task_spec=task_spec, actor_role="controller", capability=self.controller_capability)
        if start_task:
            self.auth.acquire_lease(task_spec.unique_key, worker_id=f"worker-{task_id}", ttl_seconds=60)
        return task_spec, acc_spec, spec_hash

    def test_s30_acceptance_spec_tampered_in_db_and_after_start_rejected(self) -> None:
        task_spec, acc_spec, _ = self._make_task_and_spec("t-s26-tamper")
        # Tamper criteria directly in SQLite
        with self.store.transaction() as conn:
            row = conn.execute("SELECT spec_json FROM acceptance_specs WHERE task_unique_key = ?;", (task_spec.unique_key,)).fetchone()
            tampered = json.loads(row["spec_json"])
            tampered["criteria"]["min_pass"] = 0
            conn.execute("UPDATE acceptance_specs SET spec_json = ? WHERE task_unique_key = ?;", (json.dumps(tampered), task_spec.unique_key))

        with self.assertRaises(VerificationFailedError):
            self.acceptance_reg.load_and_verify_spec(task_spec=task_spec, expected_policy_hash=self.policy_hash)

    def test_s30_worker_forged_pass_and_spec_modification_rejected(self) -> None:
        task_spec, acc_spec, _ = self._make_task_and_spec("t-s26-worker")
        # Worker cannot register or modify AcceptanceSpec
        with self.assertRaises(PolicyDeniedError):
            self.acceptance_reg.register_spec(acc_spec, task_spec=task_spec, actor_role="worker")

        # Worker cannot submit PASS pretending to be independent verifier
        with self.assertRaises(PolicyDeniedError):
            self.acceptance_reg.assert_verifier_authority(
                actor_role="worker",
                verifier_id=acc_spec.verifier_id,
                trusted_spec=acc_spec,
            )

    def test_s30_acceptance_spec_cross_task_and_cross_project_replay_rejected(self) -> None:
        task_a, acc_a, hash_a = self._make_task_and_spec("t-cross-a", project_id="proj-a")
        task_b, _, _ = self._make_task_and_spec("t-cross-b", project_id="proj-b")
        # Attempt to verify task_b expecting task_a's spec hash
        with self.assertRaises(VerificationFailedError):
            self.acceptance_reg.load_and_verify_spec(
                task_spec=task_b,
                expected_policy_hash=self.policy_hash,
                expected_spec_hash=hash_a,
            )
        # Attempt to verify task_a with wrong policy_hash
        with self.assertRaises(VerificationFailedError):
            self.acceptance_reg.load_and_verify_spec(
                task_spec=task_a,
                expected_policy_hash="0" * 64,
            )

    def test_s30_expired_acceptance_spec_rejected(self) -> None:
        task_spec, _, _ = self._make_task_and_spec("t-s26-exp", expires_at_utc="2026-01-01T00:00:10+00:00")
        self.clock.advance(seconds=30)
        with self.assertRaises(VerificationFailedError):
            self.acceptance_reg.load_and_verify_spec(task_spec=task_spec, expected_policy_hash=self.policy_hash)

    def test_s30_evidence_content_replaced_and_hash_mismatch_detected(self) -> None:
        rec = self.evidence_store.store_bytes(b"trusted log output", kind=EvidenceKind.EXECUTION_LOG, actor_role="verifier")
        self.assertTrue(self.evidence_store.verify_digest(rec.digest))
        # Replace content on disk
        obj_path = self.base / "cas" / rec.relative_path
        obj_path.write_bytes(b"tampered log output")
        with self.assertRaises(EvidenceIntegrityError):
            self.evidence_store.read_bytes(rec.digest)
        with self.assertRaises(EvidenceIntegrityError):
            self.evidence_store.verify_digest(rec.digest)
        # Deduplication write must also detect corrupted existing object
        with self.assertRaises(EvidenceIntegrityError):
            self.evidence_store.store_bytes(b"trusted log output", kind=EvidenceKind.EXECUTION_LOG, actor_role="verifier")

    def test_s30_evidence_store_path_traversal_symlink_and_hardlink_rejected(self) -> None:
        with self.assertRaises(EvidenceIntegrityError):
            self.evidence_store.read_bytes("../../etc/passwd")

        rec = self.evidence_store.store_bytes(b"clean artifact", kind=EvidenceKind.RAW_EVIDENCE)
        obj_path = self.base / "cas" / rec.relative_path

        # Hardlink attack
        outside = self.base / "outside_hardlink"
        try:
            outside.hardlink_to(obj_path)
        except OSError:
            pass
        else:
            with self.assertRaises(EvidenceIntegrityError):
                self.evidence_store.read_bytes(rec.digest)
            outside.unlink()

        # Symlink attack
        fake_digest = hashlib.sha256(b"sym").hexdigest()
        sym_dir = self.base / "cas" / "evidence" / "sha256" / fake_digest[:2]
        sym_dir.mkdir(parents=True, exist_ok=True)
        target_outside = self.base / "secret_target.txt"
        target_outside.write_text("sym-secret", encoding="utf-8")
        try:
            (sym_dir / fake_digest).symlink_to(target_outside)
        except OSError:
            self.skipTest("symlink unavailable")
        with self.assertRaises(EvidenceIntegrityError):
            self.evidence_store.read_bytes(fake_digest)

    def test_s30_concurrent_evidence_writes_and_partial_staging_recovery(self) -> None:
        payload = b"concurrent identical evidence payload"
        barrier = threading.Barrier(4)
        results = []
        errors = []
        lock = threading.Lock()

        def write_worker() -> None:
            barrier.wait()
            try:
                r = self.evidence_store.store_bytes(payload, kind=EvidenceKind.ACCEPTANCE_REPORT, actor_role="controller")
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=write_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(len({r.digest for r in results}), 1)
        self.assertTrue(any(not r.deduplicated for r in results))

        # Partial write interruption leaves staging .tmp file; recovery cleans it without touching committed blob
        class BrokenStream:
            def __init__(self) -> None:
                self._sent = False

            def read(self, _n: int = -1) -> bytes:
                if not self._sent:
                    self._sent = True
                    return b"partial-"
                raise IOError("simulated crash")

        with self.assertRaises(IOError):
            self.evidence_store.store_stream(BrokenStream(), kind=EvidenceKind.EXECUTION_LOG)

        orphan_tmp = self.base / "cas" / "evidence" / ".staging" / ".tmp-aborted"
        orphan_tmp.write_bytes(b"half-written")
        cleaned = self.evidence_store.recover_orphaned_staging()
        self.assertIn(".tmp-aborted", cleaned)
        self.assertTrue(self.evidence_store.verify_digest(results[0].digest))

    def test_s30_secret_credential_leak_and_worker_overwrite_rejected(self) -> None:
        with self.assertRaises(PolicyDeniedError):
            self.evidence_store.store_bytes(b"OPENAI_API_KEY=sk-1234567890abcdefghijklmnop", kind=EvidenceKind.EXECUTION_LOG)
        with self.assertRaises(PolicyDeniedError):
            self.evidence_store.store_bytes(b"token=ghp_123456789012345678901234567890123456", kind=EvidenceKind.RAW_EVIDENCE)
        with self.assertRaises(PolicyDeniedError):
            self.evidence_store.store_bytes(b"worker tamper attempt", kind=EvidenceKind.RAW_EVIDENCE, actor_role="worker")

    def test_s30_manifest_lease_epoch_git_sha_env_verifier_and_replay_protections(self) -> None:
        task_spec, acc_spec, _ = self._make_task_and_spec("t-s29-full", start_task=False)
        lease = self.auth.acquire_lease(task_spec.unique_key, worker_id="worker-r4", ttl_seconds=60)

        log_rec = self.evidence_store.store_bytes(b"83/83 PASS", kind=EvidenceKind.EXECUTION_LOG, actor_role="verifier")
        git_state = capture_trusted_git_state(self.repo_dir, base_sha=self.base_sha, evidence_store=self.evidence_store)
        env_rec = ExecutionEnvironmentRecord.capture(
            repo_root=self.repo_dir,
            sandbox_backend="linux-bwrap",
            sandbox_version="0.8.0",
            test_command="PYTHONPATH=src python3 -m unittest discover -s tests -v",
            exit_code=0,
            passed_count=10,
            failed_count=0,
            skipped_count=0,
        )

        with self.assertRaises(PolicyDeniedError):
            self.manifest_reg.create_and_persist_manifest(
                task_spec=task_spec, execution_id=lease.execution_id,
                worker_id="worker-r4", fencing_epoch=lease.fencing_epoch,
                workspace_id="ws-1", git_state=git_state, environment=env_rec,
                artifact_hashes={}, verifier_id=acc_spec.verifier_id, actor_role="verifier",
            )

        # Forged Git SHA rejected
        with self.assertRaises(VerificationFailedError):
            capture_trusted_git_state(
                self.repo_dir,
                base_sha=self.base_sha,
                evidence_store=self.evidence_store,
                claimed_commit_sha="f" * 40,
            )

        # Wrong fencing_epoch rejected
        with self.assertRaises(StaleEpochError):
            self.manifest_reg.create_and_persist_manifest(
                capability=self.verifier_capability,
                task_spec=task_spec,
                execution_id=lease.execution_id,
                worker_id="worker-r4",
                fencing_epoch=lease.fencing_epoch + 1,
                workspace_id="ws-1",
                git_state=git_state,
                environment=env_rec,
                artifact_hashes={"test_log": log_rec.digest},
                verifier_id=acc_spec.verifier_id,
            )

        # Forged verifier_id rejected
        with self.assertRaises(VerificationFailedError):
            self.manifest_reg.create_and_persist_manifest(
                capability=self.verifier_capability,
                task_spec=task_spec,
                execution_id=lease.execution_id,
                worker_id="worker-r4",
                fencing_epoch=lease.fencing_epoch,
                workspace_id="ws-1",
                git_state=git_state,
                environment=env_rec,
                artifact_hashes={"test_log": log_rec.digest},
                verifier_id="verifier-impostor",
            )

        # Persist valid manifest
        manifest = self.manifest_reg.create_and_persist_manifest(
            capability=self.verifier_capability,
            task_spec=task_spec,
            execution_id=lease.execution_id,
            worker_id="worker-r4",
            fencing_epoch=lease.fencing_epoch,
            workspace_id="ws-1",
            git_state=git_state,
            environment=env_rec,
            artifact_hashes={"test_log": log_rec.digest},
            verifier_id=acc_spec.verifier_id,
        )

        # Cross-execution replay rejected
        with self.assertRaises(VerificationFailedError):
            self.manifest_reg.verify_manifest(
                manifest.evidence_id,
                task_spec=task_spec,
                expected_execution_id="other-exec-id",
                expected_fencing_epoch=lease.fencing_epoch,
                expected_policy_hash=self.policy_hash,
                expected_git_state=git_state,
                expected_environment=env_rec,
            )

        # Execution environment mismatch invalidates old manifest
        altered_env = ExecutionEnvironmentRecord(**{**env_rec.to_dict(), "python_version": "3.99.0"})
        with self.assertRaises(VerificationFailedError):
            self.manifest_reg.verify_manifest(
                manifest.evidence_id,
                task_spec=task_spec,
                expected_execution_id=lease.execution_id,
                expected_fencing_epoch=lease.fencing_epoch,
                expected_policy_hash=self.policy_hash,
                expected_git_state=git_state,
                expected_environment=altered_env,
            )

        # Evidence missing / DB inconsistency blocks verification
        (self.base / "cas" / log_rec.relative_path).unlink()
        with self.assertRaises(VerificationFailedError):
            self.manifest_reg.verify_manifest(
                manifest.evidence_id,
                task_spec=task_spec,
                expected_execution_id=lease.execution_id,
                expected_fencing_epoch=lease.fencing_epoch,
                expected_policy_hash=self.policy_hash,
                expected_git_state=git_state,
                expected_environment=env_rec,
            )

        # Expired lease rejected
        self.clock.advance(seconds=120)
        with self.assertRaises(LeaseExpiredError):
            self.manifest_reg.create_and_persist_manifest(
                capability=self.verifier_capability,
                task_spec=task_spec,
                execution_id=lease.execution_id,
                worker_id="worker-r4",
                fencing_epoch=lease.fencing_epoch,
                workspace_id="ws-1",
                git_state=git_state,
                environment=env_rec,
                artifact_hashes={},
                verifier_id=acc_spec.verifier_id,
            )

    def test_manifest_rejects_forged_base_sha(self):
        from dataclasses import replace
        task, acc, _ = self._make_task_and_spec("forged-base", start_task=False)
        lease = self.auth.acquire_lease(task.unique_key, worker_id="w", ttl_seconds=60)
        state = capture_trusted_git_state(self.repo_dir, base_sha=self.base_sha, evidence_store=self.evidence_store)
        env = ExecutionEnvironmentRecord.capture(repo_root=self.repo_dir, sandbox_backend="mock", sandbox_version="1", test_command="test", exit_code=0, passed_count=1, failed_count=0, skipped_count=0)
        with self.assertRaises(VerificationFailedError):
            self.manifest_reg.create_and_persist_manifest(capability=self.verifier_capability, task_spec=task, execution_id=lease.execution_id, worker_id="w", fencing_epoch=lease.fencing_epoch, workspace_id="ws", git_state=replace(state, base_sha="f"*40), environment=env, artifact_hashes={}, verifier_id=acc.verifier_id)

    def test_s30_uncommitted_changes_require_snapshot_and_cannot_masquerade_as_head(self) -> None:
        (self.repo_dir / "uncommitted.py").write_text("print('dirty')\n", encoding="utf-8")
        with self.assertRaises(VerificationFailedError):
            capture_trusted_git_state(
                self.repo_dir,
                base_sha=self.base_sha,
                evidence_store=self.evidence_store,
                claimed_clean=True,
            )
        dirty_state = capture_trusted_git_state(
            self.repo_dir,
            base_sha=self.base_sha,
            evidence_store=self.evidence_store,
        )
        self.assertTrue(dirty_state.dirty_workspace)
        self.assertIsNotNone(dirty_state.uncommitted_snapshot_hash)
        self.assertTrue(self.evidence_store.verify_digest(dirty_state.uncommitted_snapshot_hash))

    def test_r4_role_string_is_not_registration_authority(self):
        task, spec, _ = self._make_task_and_spec("role-spoof", start_task=False)
        for role in ("controller", "verifier"):
            with self.assertRaises(PolicyDeniedError):
                self.acceptance_reg.register_spec(spec, task_spec=task, actor_role=role)

    def test_sealed_completion_rolls_back_and_replays(self):
        from dataclasses import replace
        from opencognitiveos.contracts.models import EffectOutcome, EffectReceipt
        from opencognitiveos.execution.worktree import WorkspaceManager
        from opencognitiveos.providers.mock_provider import MockProvider
        from opencognitiveos.verification.completion import EvidenceCompletion

        native = os.environ.get("OCOS_NATIVE_SEALED_TEST") == "1"
        command = "verify-file app.py" if native else "fixture verifier"
        task, spec, _ = self._make_task_and_spec("sealed", start_task=False, command=command)
        manager = WorkspaceManager(authority=self.auth, repo_path=self.repo_dir, workspaces_root=self.base / "trees")
        ws = manager.create_workspace(workspace_id="ws", repo_id=task.repo_id, task_id=task.task_id, base_sha=self.base_sha)
        lease = self.auth.acquire_lease(task.unique_key, worker_id="w")
        fence = dict(worker_id="w", execution_id=lease.execution_id, epoch=lease.fencing_epoch)
        rec = self.auth.get_task(task.unique_key)
        rec = self.auth.transition_task(task.unique_key, TaskStatus.RUNNING, expected_revision=rec.revision, **fence)
        output = MockProvider().infer({"run_id": lease.execution_id, "goal": task.goal, "tool": "mock.echo"})
        sub = self.auth.record_tool_intent(task.unique_key, tool="mock.echo", args=output["proposed_args"], **fence)
        self.auth.record_effect_receipt(task.unique_key, EffectReceipt(sub.intent.intent_id, EffectOutcome.SUCCEEDED,
                                       "fixture", "fixture"), **fence)
        rec = self.auth.transition_task(task.unique_key, TaskStatus.VERIFYING, expected_revision=rec.revision, **fence)
        wt = Path(ws.worktree_path)
        gate = EvidenceCompletion(manager, self.evidence_store, self.spec_key, self.manifest_key, self.policy_hash)
        if native:
            from opencognitiveos.verification.file_verifier import verify_file
            self.auth.evidence_completion = gate
            manifest = verify_file(self.auth, task_key=task.unique_key, workspace_id=ws.workspace_id, **fence)
            report = self.evidence_store.read_bytes(manifest.artifact_hashes["test_log"])
            self.assertIn(b"verified candidate blob", report)
            print(report.decode(), flush=True)
        else:
            git = capture_trusted_git_state(wt, base_sha=self.base_sha, evidence_store=self.evidence_store)
            env = ExecutionEnvironmentRecord.capture(repo_root=wt, sandbox_backend="linux-bwrap", sandbox_version="fixture",
                        test_command=spec.criteria["command"], exit_code=0, passed_count=1, failed_count=0, skipped_count=0)
            log = self.evidence_store.store_bytes(b"fixture verifier log", kind=EvidenceKind.EXECUTION_LOG)
            manifest = self.manifest_reg.create_and_persist_manifest(task_spec=task, worker_id="w",
                        execution_id=lease.execution_id, fencing_epoch=lease.fencing_epoch, workspace_id="ws", git_state=git,
                        environment=env, artifact_hashes={"test_log": log.digest}, verifier_id=spec.verifier_id,
                        capability=self.verifier_capability)
        log_digest = manifest.artifact_hashes["test_log"]
        kwargs = dict(**fence, expected_revision=rec.revision, output_payload=output, evidence_id=manifest.evidence_id)
        self.auth.evidence_completion = replace(gate, verifier_key=secrets.token_bytes(32))
        with self.assertRaises(VerificationFailedError):
            self.auth.verify_and_complete(task.unique_key, **kwargs)
        self.auth.evidence_completion = gate
        def interrupt(_conn):
            raise RuntimeError("interrupted")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.auth.verify_and_complete(task.unique_key, before_commit_hook=interrupt, **kwargs)
        self.assertEqual(self.auth.get_task(task.unique_key).status, TaskStatus.VERIFYING)
        with self.store.open_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM verification_receipts").fetchone()[0], 0)
        final, _ = self.auth.verify_and_complete(task.unique_key, **kwargs)
        self.assertEqual(final.status, TaskStatus.SUCCEEDED)
        self.assertTrue(self.auth.replay_verification(task.unique_key))
        self.assertTrue(self.auth.audit_database_consistency(task.unique_key)["ok"])
        from opencognitiveos.kernel.knowledge import KnowledgeGraph
        curator = object()
        graph = KnowledgeGraph(self.auth, controller_capability=curator)
        args = dict(task=task.unique_key, subject="claim", target="observation", capability=curator)
        self.assertEqual(graph.append(relation="supports", **args), graph.append(relation="supports", **args))
        graph.append(relation="contradicts", **args)
        edges = graph.history(project=task.project_id, repo=task.repo_id, subject="claim")
        self.assertEqual([e["relation"] for e in edges], ["supports", "contradicts"])
        self.assertEqual(graph.history(project=task.project_id, repo=task.repo_id, subject="claim",
                                       through_sequence=edges[0]["seq"]), edges[:1])
        if os.name == "posix":
            from opencognitiveos.interfaces.cli import main
            config = dict(repo=str(self.repo_dir), workspaces=str(self.base / "trees"),
                          cas=str(self.base / "cas"), policy_hash=self.policy_hash)
            for name, key in (("controller", self.spec_key), ("verifier", self.manifest_key)):
                path = (self.base / name).resolve()
                path.write_bytes(key)
                path.chmod(0o600)
                config[name + "_key_file"] = str(path)
            path = (self.base / "config.json").resolve()
            path.write_text(json.dumps(config), encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(main(["--db", str(self.db_path), "replay-evidence", "--unique-key", task.unique_key,
                                   "--config", str(path)]), 0)
        from opencognitiveos.storage.evidence_backup import export_snapshot, restore_snapshot
        snapshot = self.base / "backup"
        restored = self.base / "restored"
        pin = export_snapshot(self.store, self.evidence_store, snapshot)
        restore_snapshot(snapshot, restored, pin)
        recovered = ExecutionAuthority(SQLiteStore(restored / "state.sqlite3"), clock=self.clock)
        recovered.evidence_completion = replace(gate, cas=ContentAddressedEvidenceStore(restored / "cas"))
        self.assertTrue(recovered.replay_verification(task.unique_key))
        (snapshot / "objects" / log_digest).write_bytes(b"corrupted")
        with self.assertRaises(DatabaseIntegrityError):
            restore_snapshot(snapshot, self.base / "bad-restore", pin)
        self.assertFalse((self.base / "bad-restore").exists())
        (self.evidence_store.evidence_root / log_digest[:2] / log_digest).unlink()
        self.assertFalse(self.auth.replay_verification(task.unique_key))

    def test_r4_legacy_completion_denied(self):
        task, _, _ = self._make_task_and_spec("legacy-bypass", start_task=False)
        lease = self.auth.acquire_lease(task.unique_key, worker_id="worker", ttl_seconds=60)
        rec = self.auth.get_task(task.unique_key)
        with self.assertRaisesRegex(VerificationFailedError, "legacy completion denied"):
            self.auth.verify_and_complete(task.unique_key, worker_id="worker",
                execution_id=lease.execution_id, epoch=lease.fencing_epoch,
                expected_revision=rec.revision)

    def test_r4_forged_task_spec_denied(self):
        from dataclasses import replace
        task, spec, _ = self._make_task_and_spec("forged-task", start_task=False)
        with self.assertRaisesRegex(VerificationFailedError, "persisted task"):
            self.acceptance_reg.register_spec(spec, task_spec=replace(task, goal="forged"), capability=self.controller_capability)

    def test_s30_unauthorized_verification_state_transition_blocked(self) -> None:
        task_spec, _, _ = self._make_task_and_spec("t-s30-state")
        rec = self.auth.get_task(task_spec.unique_key)
        # Worker cannot jump directly from RUNNING to SUCCEEDED bypassing IndependentVerifier
        with self.assertRaises((InvalidTransitionError, PolicyDeniedError)):
            self.auth.transition_task(
                task_spec.unique_key,
                TaskStatus.SUCCEEDED,
                expected_revision=rec.revision,
                actor_role="worker",
            )


if __name__ == "__main__":
    unittest.main()
