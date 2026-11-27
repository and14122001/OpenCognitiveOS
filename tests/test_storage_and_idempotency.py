"""Tests for SQLite migrations, integrity checks, backup/restore, and concurrent submissions."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from opencognitiveos.contracts.models import InjectableClock, TaskSpec, canonical_json_hash
from opencognitiveos.errors import TaskConflictError
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.storage.sqlite_store import MIGRATIONS, SQLiteStore


def _make_spec(task_id: str = "t-1", goal: str = "verify sqlite") -> TaskSpec:
    return TaskSpec(
        project_id="proj-1",
        repo_id="and14122001/OpenCognitiveOS",
        task_id=task_id,
        goal=goal,
        acceptance_hash=canonical_json_hash({"goal": goal}),
    )


class TestStorageAndIdempotency(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_migrations_idempotent_and_pragmas(self) -> None:
        first = self.store.migrate()
        self.assertEqual(first, [m.version for m in MIGRATIONS])
        second = self.store.migrate()
        self.assertEqual(second, [])
        self.assertEqual(self.store.schema_version(), MIGRATIONS[-1].version)

        integrity = self.store.integrity_check()
        self.assertTrue(integrity["ok"])
        self.assertEqual(integrity["journal_mode"], "wal")
        self.assertEqual(integrity["foreign_keys"], 1)

    def test_unique_key_idempotent_and_conflict_rejection(self) -> None:
        self.store.migrate()
        auth = ExecutionAuthority(self.store, clock=self.clock)
        spec1 = _make_spec("t-dup", "goal A")
        rec1, created1 = auth.submit_task(spec1)
        self.assertTrue(created1)
        self.assertEqual(rec1.unique_key, "and14122001/OpenCognitiveOS::t-dup")

        # Exact same content -> idempotent return
        rec2, created2 = auth.submit_task(spec1)
        self.assertFalse(created2)
        self.assertEqual(rec2.revision, rec1.revision)

        # Same repo_id::task_id with different content -> rejected
        spec_diff = _make_spec("t-dup", "goal B (tampered)")
        with self.assertRaises(TaskConflictError):
            auth.submit_task(spec_diff)

    def test_concurrent_multi_connection_task_submission(self) -> None:
        self.store.migrate()
        spec = _make_spec("t-concurrent", "concurrent submit")
        barrier = threading.Barrier(6)
        results: list[tuple[str, bool]] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            local_store = SQLiteStore(self.db_path, clock=self.clock)
            local_auth = ExecutionAuthority(local_store, clock=self.clock)
            barrier.wait()
            try:
                rec, created = local_auth.submit_task(spec)
                with lock:
                    results.append((rec.unique_key, created))
            except Exception as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        created_count = sum(1 for _, c in results if c)
        self.assertEqual(created_count, 1)

    def test_backup_and_restore(self) -> None:
        self.store.migrate()
        auth = ExecutionAuthority(self.store, clock=self.clock)
        spec = _make_spec("t-backup", "persist before backup")
        auth.submit_task(spec)

        backup_path = Path(self.tmpdir.name) / "backup.db"
        self.store.backup_to(backup_path)

        restored_path = Path(self.tmpdir.name) / "restored.db"
        restored_store = SQLiteStore.restore_from(backup_path, restored_path, clock=self.clock)
        restored_auth = ExecutionAuthority(restored_store, clock=self.clock)
        rec = restored_auth.get_task(spec.unique_key)
        self.assertEqual(rec.spec.goal, "persist before backup")
        self.assertTrue(restored_store.integrity_check()["ok"])


if __name__ == "__main__":
    unittest.main()
