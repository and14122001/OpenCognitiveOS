"""Tests for dual-worker lease contention, lease expiration, and fencing epoch."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from opencognitiveos.contracts.models import EffectOutcome, EffectReceipt, InjectableClock, TaskSpec, TaskStatus, canonical_json_hash
from opencognitiveos.errors import LeaseConflictError, LeaseExpiredError, StaleEpochError
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.storage.sqlite_store import SQLiteStore


class TestConcurrencyAndFencing(unittest.TestCase):
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
            task_id="t-fence",
            goal="test fencing",
            acceptance_hash=canonical_json_hash({"goal": "test fencing"}),
        )
        rec, _ = self.auth.submit_task(self.spec)
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        self.ready_rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_two_workers_compete_for_lease(self) -> None:
        barrier = threading.Barrier(2)
        acquired: list[str] = []
        conflicts: list[Exception] = []
        lock = threading.Lock()

        def try_acquire(worker_id: str) -> None:
            local_auth = ExecutionAuthority(SQLiteStore(self.db_path, clock=self.clock), clock=self.clock)
            barrier.wait()
            try:
                lease = local_auth.acquire_lease(self.spec.unique_key, worker_id=worker_id)
                with lock:
                    acquired.append(lease.worker_id)
            except LeaseConflictError as exc:
                with lock:
                    conflicts.append(exc)

        t1 = threading.Thread(target=try_acquire, args=("worker-A",))
        t2 = threading.Thread(target=try_acquire, args=("worker-B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(conflicts), 1)

    def test_stale_worker_rejected_after_lease_expiration_and_epoch_advance(self) -> None:
        lease1 = self.auth.acquire_lease(self.spec.unique_key, worker_id="worker-old", ttl_seconds=60)
        running = self.auth.transition_task(
            self.spec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=self.ready_rec.revision,
            actor_role="worker",
            epoch=lease1.fencing_epoch,
            worker_id="worker-old",
            execution_id=lease1.execution_id,
        )

        # Advance injectable clock past lease TTL without sleep
        self.clock.advance(61)

        with self.assertRaises(LeaseExpiredError):
            self.auth.heartbeat_lease(
                self.spec.unique_key, worker_id="worker-old", epoch=lease1.fencing_epoch, execution_id=lease1.execution_id
            )

        with self.assertRaises(LeaseExpiredError):
            self.auth.record_tool_intent(
                self.spec.unique_key,
                worker_id="worker-old",
                execution_id=lease1.execution_id,
                epoch=lease1.fencing_epoch,
                tool="mock.echo",
                args={"x": 1},
            )

        # New worker steals expired lease, advancing epoch to 2
        lease2 = self.auth.acquire_lease(self.spec.unique_key, worker_id="worker-new", ttl_seconds=60)
        self.assertEqual(lease2.fencing_epoch, lease1.fencing_epoch + 1)

        # Old worker with epoch 1 is fenced out on state, intent, and receipt writes
        with self.assertRaises(StaleEpochError):
            self.auth.transition_task(
                self.spec.unique_key,
                TaskStatus.VERIFYING,
                expected_revision=running.revision,
                actor_role="worker",
                epoch=lease1.fencing_epoch,
                worker_id="worker-old",
                execution_id=lease1.execution_id,
            )

        with self.assertRaises(StaleEpochError):
            self.auth.record_effect_receipt(
                self.spec.unique_key,
                EffectReceipt(
                    intent_id="non-existent",
                    outcome=EffectOutcome.SUCCEEDED,
                    external_ref="ref",
                    evidence_hash="abc",
                ),
                worker_id="worker-old",
                epoch=lease1.fencing_epoch,
                execution_id=lease1.execution_id,
            )


if __name__ == "__main__":
    unittest.main()
