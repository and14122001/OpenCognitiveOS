"""Tests for SQLite transaction rollback, outbox restart recovery, and consumer deduplication."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opencognitiveos.contracts.models import InjectableClock, TaskSpec, TaskStatus, canonical_json_hash
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.storage.sqlite_store import SQLiteStore


class TestOutboxAndRestart(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)
        self.spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-outbox",
            goal="outbox atomicity",
            acceptance_hash=canonical_json_hash({"goal": "outbox atomicity"}),
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_transaction_interruption_rolls_back_state_events_and_outbox(self) -> None:
        rec, _ = self.auth.submit_task(self.spec)
        with self.store.open_connection() as conn:
            events_before = conn.execute("SELECT COUNT(*) FROM events;").fetchone()[0]
            outbox_before = conn.execute("SELECT COUNT(*) FROM outbox;").fetchone()[0]

        def crash_before_commit(_conn) -> None:
            raise RuntimeError("Simulated power loss before COMMIT")

        with self.assertRaises(RuntimeError):
            self.auth.transition_task(
                rec.unique_key,
                TaskStatus.PLANNED,
                expected_revision=rec.revision,
                actor_role="controller", controller_capability=self.auth._controller_capability,
                before_commit_hook=crash_before_commit,
            )

        after = self.auth.get_task(rec.unique_key)
        self.assertEqual(after.status, TaskStatus.SUBMITTED)
        self.assertEqual(after.revision, rec.revision)
        with self.store.open_connection() as conn:
            events_after = conn.execute("SELECT COUNT(*) FROM events;").fetchone()[0]
            outbox_after = conn.execute("SELECT COUNT(*) FROM outbox;").fetchone()[0]
        self.assertEqual(events_after, events_before)
        self.assertEqual(outbox_after, outbox_before)

    def test_restart_dispatches_pending_outbox_and_deduplicates_redelivery(self) -> None:
        rec, _ = self.auth.submit_task(self.spec)
        self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )

        restarted_store = SQLiteStore(self.db_path, clock=self.clock)
        restarted_auth = ExecutionAuthority(restarted_store, clock=self.clock)

        delivered_ids: list[str] = []
        count1 = restarted_auth.dispatch_outbox(
            "audit-consumer", lambda msg: delivered_ids.append(msg["event_id"])
        )
        self.assertEqual(count1, 2)
        self.assertEqual(len(delivered_ids), 2)

        count2 = restarted_auth.dispatch_outbox(
            "audit-consumer",
            lambda msg: delivered_ids.append(msg["event_id"]),
            include_dispatched=True,
        )
        self.assertEqual(count2, 0)
        self.assertEqual(len(delivered_ids), 2)


if __name__ == "__main__":
    unittest.main()
