"""M0-M1.6 Seal Tests: exact result_json tampering (add/delete/modify/type-error) and single-transaction audit."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from opencognitiveos import __version__
from opencognitiveos.contracts.models import InjectableClock, TaskSpec, TaskStatus, canonical_json_hash
from opencognitiveos.errors import DatabaseIntegrityError
from opencognitiveos.execution.runner import OfflineRunner
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.providers.mock_provider import MockProvider
from opencognitiveos.storage.sqlite_store import SQLiteStore


class TestM16Seal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos_m16.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _run_succeeded(self, task_id: str) -> tuple[TaskSpec, dict]:
        goal = f"goal-{task_id}"
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id=task_id,
            goal=goal,
            acceptance_hash=canonical_json_hash({"goal": goal}),
        )
        rec = OfflineRunner(self.auth, MockProvider()).run_task(spec)
        self.assertEqual(rec.status, TaskStatus.SUCCEEDED)
        self.assertTrue(self.auth.audit_database_consistency(spec.unique_key)["ok"])
        return spec, dict(rec.result or {})

    def test_result_json_tampering_add_delete_modify_type_errors(self) -> None:
        spec, orig = self._run_succeeded("m16-res-tamper")

        mutations: list[tuple[str, str]] = [
            # Modify each security-relevant field
            ("modify_output_hash", json.dumps({**orig, "output_hash": "tampered"})),
            ("modify_verification_id", json.dumps({**orig, "verification_id": "tampered"})),
            ("modify_verification_evidence_hash", json.dumps({**orig, "verification_evidence_hash": "tampered"})),
            # Add extra field
            ("add_extra_field", json.dumps({**orig, "extra_injected": "evil"})),
            # Delete each field
            ("delete_output_hash", json.dumps({k: v for k, v in orig.items() if k != "output_hash"})),
            ("delete_verification_id", json.dumps({k: v for k, v in orig.items() if k != "verification_id"})),
            (
                "delete_verification_evidence_hash",
                json.dumps({k: v for k, v in orig.items() if k != "verification_evidence_hash"}),
            ),
            # Wrong types & malformed JSON
            ("type_int_output_hash", json.dumps({**orig, "output_hash": 12345})),
            ("type_list_root", json.dumps(["not", "a", "dict"])),
            ("type_str_root", json.dumps("not-a-dict")),
            ("type_null_root", "null"),
            ("malformed_json", "{not-valid-json"),
        ]

        for label, bad_json in mutations:
            with self.subTest(mutation=label):
                with self.store.transaction() as conn:
                    conn.execute(
                        "UPDATE tasks SET result_json = ? WHERE unique_key = ?;",
                        (bad_json, spec.unique_key),
                    )
                with self.assertRaises(DatabaseIntegrityError):
                    self.auth.audit_database_consistency(spec.unique_key)

        # Restore original result_json -> passes again
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET result_json = ? WHERE unique_key = ?;",
                (json.dumps(orig, sort_keys=True), spec.unique_key),
            )
        self.assertTrue(self.auth.audit_database_consistency(spec.unique_key)["ok"])

    def test_version_is_0_1_6_dev1(self) -> None:
        self.assertEqual(__version__, "0.1.6.dev1")


if __name__ == "__main__":
    unittest.main()
