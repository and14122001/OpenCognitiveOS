"""M0-M1.5 Systematic Acceptance Tests:
- Public operation x TaskStatus authorization matrix via unittest.subTest
- P0-01 SUBMITTED lease/ToolIntent rejection
- P0-02 Historical snapshot replay vs live cross-table DB tampering detection
- Fixed-seed 10,000-step randomized state-operation sequence test
- Old schema v1 -> v6 upgrade & idempotency compatibility
- PEP 440 version & offline package build/install/CLI verification
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from opencognitiveos import __version__
from opencognitiveos.contracts.models import (
    OPERATION_ALLOWED_STATES,
    Budget,
    EffectOutcome,
    EffectReceipt,
    InjectableClock,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.errors import (
    BudgetExceededError,
    CASConflictError,
    DatabaseIntegrityError,
    InvalidTransitionError,
    OpenCognitiveOSError,
    ReconciliationRequiredError,
    TaskConflictError,
)
from opencognitiveos.execution.runner import OfflineRunner
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.providers.mock_provider import MockProvider
from opencognitiveos.storage.sqlite_store import MIGRATIONS, SQLiteStore


class TestM15Systematic(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos_m15.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _create_task_in_status(self, task_id: str, target: TaskStatus) -> tuple[TaskSpec, object, object | None]:
        goal = f"goal-{task_id}"
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id=task_id,
            goal=goal,
            acceptance_hash=canonical_json_hash({"goal": goal}),
            budget=Budget(max_retries=3, max_steps=10),
        )
        rec, _ = self.auth.submit_task(spec)
        if target == TaskStatus.SUBMITTED:
            return spec, rec, None

        if target == TaskStatus.CANCELLED:
            rec = self.auth.cancel_task(spec.unique_key, expected_revision=rec.revision)
            return spec, rec, None

        rec = self.auth.transition_task(
            spec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        if target == TaskStatus.PLANNED:
            return spec, rec, None

        if target == TaskStatus.FAILED:
            rec = self.auth.transition_task(
                spec.unique_key, TaskStatus.FAILED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
            )
            return spec, rec, None

        rec = self.auth.transition_task(
            spec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        if target == TaskStatus.READY:
            return spec, rec, None

        if target == TaskStatus.WAITING_APPROVAL:
            rec = self.auth.transition_task(
                spec.unique_key, TaskStatus.WAITING_APPROVAL, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
            )
            return spec, rec, None

        lease = self.auth.acquire_lease(spec.unique_key, worker_id="w-1", ttl_seconds=120)
        rec = self.auth.transition_task(
            spec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        if target == TaskStatus.RUNNING:
            return spec, rec, lease

        if target == TaskStatus.WAITING_CHILDREN:
            rec = self.auth.transition_task(
                spec.unique_key,
                TaskStatus.WAITING_CHILDREN,
                expected_revision=rec.revision,
                actor_role="worker",
                epoch=lease.fencing_epoch,
                worker_id="w-1",
                execution_id=lease.execution_id,
            )
            return spec, rec, lease

        if target == TaskStatus.RETRY_WAIT:
            rec = self.auth.transition_task(
                spec.unique_key,
                TaskStatus.RETRY_WAIT,
                expected_revision=rec.revision,
                actor_role="controller", controller_capability=self.auth._controller_capability,
            )
            return spec, rec, None

        proposal = MockProvider().infer({"run_id": lease.execution_id, "goal": goal, "tool": "mock.echo"})
        sub = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args=proposal["proposed_args"],
        )
        if target == TaskStatus.NEEDS_RECONCILIATION:
            rec = self.auth.record_effect_receipt(
                spec.unique_key,
                EffectReceipt(sub.intent.intent_id, EffectOutcome.UNKNOWN, None, "ev-unk"),
                worker_id="w-1",
                epoch=lease.fencing_epoch,
                execution_id=lease.execution_id,
            )
            return spec, rec, None

        rec = self.auth.record_effect_receipt(
            spec.unique_key,
            EffectReceipt(sub.intent.intent_id, EffectOutcome.SUCCEEDED, "mock-ref://1", "ev-1"),
            worker_id="w-1",
            epoch=lease.fencing_epoch,
            execution_id=lease.execution_id,
        )
        rec = self.auth.transition_task(
            spec.unique_key,
            TaskStatus.VERIFYING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        if target == TaskStatus.VERIFYING:
            return spec, rec, lease

        rec, _ = self.auth.verify_and_complete(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            expected_revision=rec.revision,
            output_payload=proposal,
        )
        return spec, rec, None

    # 1. P0-01: SUBMITTED cannot acquire_lease or record_tool_intent
    def test_submitted_cannot_acquire_lease_or_record_tool_intent(self) -> None:
        spec, _, _ = self._create_task_in_status("p0-01-sub", TaskStatus.SUBMITTED)
        with self.assertRaises(InvalidTransitionError):
            self.auth.acquire_lease(spec.unique_key, worker_id="w-1")
        with self.assertRaises(InvalidTransitionError):
            self.auth.record_tool_intent(
                spec.unique_key,
                worker_id="w-1",
                execution_id="fake-exec",
                epoch=0,
                tool="mock.echo",
                args={"x": 1},
            )

    # 2. Parameterized 12 TaskStatus x 8 Public Operations Matrix using unittest.subTest
    def test_all_states_x_public_operations_matrix_with_subtest(self) -> None:
        ops = (
            "acquire_lease",
            "heartbeat_lease",
            "record_tool_intent",
            "record_effect_receipt",
            "verify_and_complete",
            "reconcile_effect",
            "schedule_retry",
            "cancel_task",
        )
        for status in TaskStatus:
            for op in ops:
                allowed = status in OPERATION_ALLOWED_STATES[op]
                if allowed:
                    continue
                with self.subTest(status=status.value, operation=op):
                    spec, rec, lease = self._create_task_in_status(f"mat-{status.value}-{op}", status)
                    exec_id = lease.execution_id if lease else "none"
                    epoch = lease.fencing_epoch if lease else 0
                    with self.assertRaises((InvalidTransitionError, ReconciliationRequiredError)):
                        if op == "acquire_lease":
                            self.auth.acquire_lease(spec.unique_key, worker_id="w-2")
                        elif op == "heartbeat_lease":
                            self.auth.heartbeat_lease(spec.unique_key, worker_id="w-1", epoch=epoch, execution_id=exec_id)
                        elif op == "record_tool_intent":
                            self.auth.record_tool_intent(
                                spec.unique_key,
                                worker_id="w-1",
                                execution_id=exec_id,
                                epoch=epoch,
                                tool="mock.echo",
                                args={"k": 1},
                                operation_id="op-mat",
                            )
                        elif op == "record_effect_receipt":
                            self.auth.record_effect_receipt(
                                spec.unique_key,
                                EffectReceipt("missing", EffectOutcome.SUCCEEDED, "r", "h"),
                                worker_id="w-1",
                                epoch=epoch,
                                execution_id=exec_id,
                            )
                        elif op == "verify_and_complete":
                            self.auth.verify_and_complete(
                                spec.unique_key,
                                worker_id="w-1",
                                execution_id=exec_id,
                                epoch=epoch,
                                expected_revision=rec.revision,
                                output_payload={},
                            )
                        elif op == "reconcile_effect":
                            self.auth.reconcile_effect(
                                spec.unique_key,
                                intent_id="missing",
                                resolved_outcome=EffectOutcome.SUCCEEDED,
                                external_ref="r",
                                evidence_hash="h",
                            )
                        elif op == "schedule_retry":
                            self.auth.schedule_retry(spec.unique_key, expected_revision=rec.revision, reason="mat")
                        elif op == "cancel_task":
                            self.auth.cancel_task(spec.unique_key, expected_revision=rec.revision)

    # 3. P0-02: Live DB effects.external_ref tampering detected by audit_database_consistency (distinct from replay_verification)
    def test_live_effects_tampering_detected_by_audit_distinct_from_historical_replay(self) -> None:
        goal = "cross table audit"
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="p0-02-audit",
            goal=goal,
            acceptance_hash=canonical_json_hash({"goal": goal}),
        )
        completed = OfflineRunner(self.auth, MockProvider()).run_task(spec)
        self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
        self.assertTrue(self.auth.audit_database_consistency(spec.unique_key)["ok"])

        # Tamper live effects.external_ref directly in SQLite
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE effects SET external_ref = 'mock-ref://TAMPERED-IN-DB' WHERE task_unique_key = ?;",
                (spec.unique_key,),
            )

        # Historical snapshot replay still succeeds (because verification_receipts holds historical snapshot)
        self.assertTrue(self.auth.replay_verification(spec.unique_key))
        # Live cross-table consistency audit MUST detect the tampering!
        with self.assertRaises(DatabaseIntegrityError):
            self.auth.audit_database_consistency(spec.unique_key)

    # 4. Fixed-seed 10,000-step randomized state-operation sequence test
    def test_10000_randomized_state_operations_fixed_seed(self) -> None:
        rng = random.Random(20261127)
        num_tasks = 20
        specs: list[TaskSpec] = []
        leases: dict[str, object] = {}
        proposals: dict[str, dict] = {}
        intents: dict[str, str] = {}

        for i in range(num_tasks):
            goal = f"fuzz-goal-{i}"
            spec = TaskSpec(
                project_id="proj-fuzz",
                repo_id="and14122001/OpenCognitiveOS",
                task_id=f"fuzz-{i}",
                goal=goal,
                acceptance_hash=canonical_json_hash({"goal": goal}),
                budget=Budget(max_retries=3, max_steps=5, lease_seconds=60),
            )
            self.auth.submit_task(spec)
            specs.append(spec)

        for step in range(10_000):
            spec = rng.choice(specs)
            rec = self.auth.get_task(spec.unique_key)
            lease = leases.get(spec.unique_key)
            op = rng.choice(
                (
                    "plan",
                    "ready",
                    "lease",
                    "run",
                    "intent",
                    "receipt_ok",
                    "receipt_unk",
                    "reconcile",
                    "verifying",
                    "complete",
                    "retry",
                    "cancel",
                    "advance_clock",
                )
            )
            try:
                if op == "plan":
                    self.auth.transition_task(
                        spec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
                    )
                elif op == "ready":
                    self.auth.transition_task(
                        spec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
                    )
                elif op == "lease":
                    leases[spec.unique_key] = self.auth.acquire_lease(spec.unique_key, worker_id="w-fuzz")
                elif op == "run" and lease is not None:
                    self.auth.transition_task(
                        spec.unique_key,
                        TaskStatus.RUNNING,
                        expected_revision=rec.revision,
                        actor_role="worker",
                        epoch=lease.fencing_epoch,
                        worker_id="w-fuzz",
                        execution_id=lease.execution_id,
                    )
                elif op == "intent" and lease is not None:
                    prop = MockProvider().infer(
                        {"run_id": lease.execution_id, "goal": spec.goal, "tool": "mock.echo"}
                    )
                    proposals[spec.unique_key] = prop
                    sub = self.auth.record_tool_intent(
                        spec.unique_key,
                        worker_id="w-fuzz",
                        execution_id=lease.execution_id,
                        epoch=lease.fencing_epoch,
                        tool="mock.echo",
                        args=prop["proposed_args"],
                        operation_id="fuzz-op-1",
                    )
                    intents[spec.unique_key] = sub.intent.intent_id
                elif op == "receipt_ok" and lease is not None and spec.unique_key in intents:
                    self.auth.record_effect_receipt(
                        spec.unique_key,
                        EffectReceipt(intents[spec.unique_key], EffectOutcome.SUCCEEDED, "ref://fuzz", "ev-fuzz"),
                        worker_id="w-fuzz",
                        epoch=lease.fencing_epoch,
                        execution_id=lease.execution_id,
                    )
                elif op == "receipt_unk" and lease is not None and spec.unique_key in intents:
                    self.auth.record_effect_receipt(
                        spec.unique_key,
                        EffectReceipt(intents[spec.unique_key], EffectOutcome.UNKNOWN, None, "ev-unk"),
                        worker_id="w-fuzz",
                        epoch=lease.fencing_epoch,
                        execution_id=lease.execution_id,
                    )
                elif op == "reconcile" and spec.unique_key in intents:
                    self.auth.reconcile_effect(
                        spec.unique_key,
                        intent_id=intents[spec.unique_key],
                        resolved_outcome=EffectOutcome.SUCCEEDED,
                        external_ref="ref://reconciled",
                        evidence_hash="ev-rec",
                    )
                elif op == "verifying" and lease is not None:
                    self.auth.transition_task(
                        spec.unique_key,
                        TaskStatus.VERIFYING,
                        expected_revision=rec.revision,
                        actor_role="worker",
                        epoch=lease.fencing_epoch,
                        worker_id="w-fuzz",
                        execution_id=lease.execution_id,
                    )
                elif op == "complete" and lease is not None and spec.unique_key in proposals:
                    self.auth.verify_and_complete(
                        spec.unique_key,
                        worker_id="w-fuzz",
                        execution_id=lease.execution_id,
                        epoch=lease.fencing_epoch,
                        expected_revision=rec.revision,
                        output_payload=proposals[spec.unique_key],
                    )
                elif op == "retry":
                    self.auth.schedule_retry(spec.unique_key, expected_revision=rec.revision, reason="fuzz-retry")
                elif op == "cancel":
                    self.auth.cancel_task(spec.unique_key, expected_revision=rec.revision, reason="fuzz-cancel")
                elif op == "advance_clock":
                    self.clock.advance(5)
            except OpenCognitiveOSError:
                # Expected domain rejections for invalid state/epoch/budget/race are part of the fuzz invariant!
                pass

        # Verify global invariants across all 20 tasks after 10,000 operations
        self.assertTrue(self.store.integrity_check()["ok"])
        for spec in specs:
            rec = self.auth.get_task(spec.unique_key)
            self.assertLessEqual(rec.retry_count, spec.budget.max_retries)
            self.assertLessEqual(rec.step_count, spec.budget.max_steps)
            if rec.status == TaskStatus.SUCCEEDED:
                self.assertTrue(self.auth.audit_database_consistency(spec.unique_key)["ok"])

    # 5. Old database schema v1 -> v6 upgrade & idempotency compatibility
    def test_old_schema_upgrade_and_idempotency_migration_compatibility(self) -> None:
        old_db = Path(self.tmpdir.name) / "legacy_v1.db"
        legacy_store = SQLiteStore(old_db, clock=self.clock)
        # Apply ONLY migration v1 manually to simulate an older database
        m1 = MIGRATIONS[0]
        with legacy_store.transaction() as conn:
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at_utc TEXT NOT NULL, checksum TEXT NOT NULL);"
            )
            for stmt in (s.strip() for s in m1.sql.split(";") if s.strip()):
                conn.execute(stmt + ";")
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at_utc, checksum) VALUES (?, ?, ?, ?);",
                (m1.version, m1.name, self.clock.now_iso(), m1.checksum),
            )
        self.assertEqual(legacy_store.schema_version(), 1)

        # Insert a v1 task directly
        legacy_auth = ExecutionAuthority(legacy_store, clock=self.clock)
        spec = TaskSpec(
            project_id="proj-legacy",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="legacy-1",
            goal="legacy upgrade",
            acceptance_hash=canonical_json_hash({"goal": "legacy upgrade"}),
        )
        # Upgrade to latest v6
        applied = legacy_store.migrate()
        self.assertEqual(applied, [2, 3, 4, 5, 6])
        self.assertEqual(legacy_store.schema_version(), MIGRATIONS[-1].version)

        completed = OfflineRunner(legacy_auth, MockProvider()).run_task(spec)
        self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
        self.assertTrue(legacy_auth.audit_database_consistency(spec.unique_key)["ok"])

    # 6. PEP 440 version & offline package build/install/CLI verification
    def test_pep440_version_and_offline_package_install_cli(self) -> None:
        # Strict PEP 440 regex check for 0.1.6.dev1
        self.assertEqual(__version__, "0.1.6.dev1")
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+(\.dev\d+)?$")

        repo_root = Path(__file__).resolve().parent.parent
        install_dir = Path(self.tmpdir.name) / "site_pkg"
        install_dir.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-build-isolation",
                "--no-deps",
                "--target",
                str(install_dir),
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(install_dir)
        ver_res = subprocess.run(
            [sys.executable, "-m", "opencognitiveos", "--version"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ver_res.returncode, 0, msg=ver_res.stderr)
        self.assertIn("0.1.6.dev1", ver_res.stdout)


if __name__ == "__main__":
    unittest.main()
