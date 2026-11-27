"""Tests for intent-first reconciliation, restart recovery, and CLI offline E2E."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from opencognitiveos.contracts.models import EffectOutcome, InjectableClock, TaskSpec, TaskStatus, canonical_json_hash
from opencognitiveos.errors import ReconciliationRequiredError
from opencognitiveos.execution.runner import OfflineRunner
from opencognitiveos.interfaces.cli import main as cli_main
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.providers.mock_provider import MockProvider
from opencognitiveos.storage.sqlite_store import SQLiteStore


class TestReconciliationAndCLIE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos.db"
        self.clock = InjectableClock()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_lost_external_response_enters_reconciliation_not_blind_retry(self) -> None:
        store = SQLiteStore(self.db_path, clock=self.clock)
        store.migrate()
        auth = ExecutionAuthority(store, clock=self.clock)
        runner = OfflineRunner(auth, MockProvider())

        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-lost",
            goal="external call with lost response",
            acceptance_hash=canonical_json_hash({"goal": "external call with lost response"}),
        )
        rec = runner.run_task(spec, simulate_lost_response=True)
        self.assertEqual(rec.status, TaskStatus.NEEDS_RECONCILIATION)

        with self.assertRaises(ReconciliationRequiredError):
            auth.acquire_lease(rec.unique_key, worker_id="w-blind")

        with store.open_connection() as conn:
            eff = conn.execute(
                "SELECT intent_id, outcome FROM effects WHERE task_unique_key = ?;", (rec.unique_key,)
            ).fetchone()
        self.assertEqual(eff["outcome"], EffectOutcome.UNKNOWN.value)

        # Reconcile moves to VERIFYING (never skips independent verification)
        reconciled = auth.reconcile_effect(
            rec.unique_key,
            intent_id=eff["intent_id"],
            resolved_outcome=EffectOutcome.SUCCEEDED,
            external_ref="ext-confirmed-123",
            evidence_hash=canonical_json_hash({"confirmed": True}),
        )
        self.assertEqual(reconciled.status, TaskStatus.VERIFYING)

    def test_restart_recovery_from_persisted_sqlite_only(self) -> None:
        store1 = SQLiteStore(self.db_path, clock=self.clock)
        store1.migrate()
        auth1 = ExecutionAuthority(store1, clock=self.clock)

        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-crash",
            goal="crash mid-run",
            acceptance_hash=canonical_json_hash({"goal": "crash mid-run"}),
        )
        rec, _ = auth1.submit_task(spec)
        rec = auth1.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=auth1._controller_capability
        )
        rec = auth1.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=auth1._controller_capability
        )
        lease = auth1.acquire_lease(rec.unique_key, worker_id="crashed-worker", ttl_seconds=30)
        auth1.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="crashed-worker",
            execution_id=lease.execution_id,
        )

        self.clock.advance(45)
        store2 = SQLiteStore(self.db_path, clock=self.clock)
        auth2 = ExecutionAuthority(store2, clock=self.clock)
        summary = auth2.recover_on_restart()
        self.assertIn(spec.unique_key, summary["recovered_to_ready"])
        recovered = auth2.get_task(spec.unique_key)
        self.assertEqual(recovered.status, TaskStatus.READY)
        self.assertEqual(recovered.retry_count, 1)

    def test_cli_offline_end_to_end(self) -> None:
        def run_cli(*args: str) -> dict:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli_main(["--db", str(self.db_path), *args])
            self.assertEqual(code, 0)
            return json.loads(buf.getvalue())

        init_out = run_cli("init-db")
        self.assertTrue(init_out["integrity"]["ok"])

        run_out = run_cli(
            "run-mock",
            "--repo-id",
            "and14122001/OpenCognitiveOS",
            "--task-id",
            "cli-1",
            "--goal",
            "offline cli e2e",
        )
        self.assertEqual(run_out["status"], "SUCCEEDED")

        status_out = run_cli("status", "--unique-key", "and14122001/OpenCognitiveOS::cli-1")
        self.assertEqual(status_out["status"], "SUCCEEDED")

        backup_dest = Path(self.tmpdir.name) / "cli-backup.db"
        backup_out = run_cli("backup", "--dest", str(backup_dest))
        self.assertTrue(Path(backup_out["backup_path"]).exists())

        chk_out = run_cli("integrity-check")
        self.assertTrue(chk_out["ok"])


if __name__ == "__main__":
    unittest.main()
