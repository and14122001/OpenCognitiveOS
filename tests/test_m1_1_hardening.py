"""M0-M1.1 Hardening Tests covering all 14 P0/P1 regression scenarios."""

from __future__ import annotations

import gc
import tempfile
import threading
import unittest
import warnings
from pathlib import Path

from opencognitiveos.contracts.models import (
    Budget,
    DataClass,
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
    InvalidTransitionError,
    LeaseExpiredError,
    PolicyDeniedError,
    StaleEpochError,
    TaskConflictError,
    TaskNotFoundError,
)
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.kernel.policy import PolicyGate
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.verifier import IndependentVerifier, VerificationFailedError


class _FakeApprovedCloudProvider:
    name = "cloud-future"
    is_cloud = True

    def capabilities(self) -> dict:
        return {"provider": self.name, "is_cloud": True}

    def infer(self, request: dict) -> dict:
        return {}

    def cancel(self, run_id: str) -> bool:
        return False

    def quota(self) -> dict:
        return {"remaining": 100, "billing_verified": True}


class TestM11Hardening(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos_harden.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _prepare_running(self, task_id: str, **spec_overrides) -> tuple[TaskSpec, object, object]:
        kwargs = {
            "project_id": "proj-1",
            "repo_id": "and14122001/OpenCognitiveOS",
            "task_id": task_id,
            "goal": f"goal-{task_id}",
            "acceptance_hash": canonical_json_hash({"goal": f"goal-{task_id}"}),
            "allowed_paths": ("src/opencognitiveos",),
            "allowed_tools": ("mock.echo",),
        }
        kwargs.update(spec_overrides)
        spec = TaskSpec(**kwargs)
        rec, _ = self.auth.submit_task(spec)
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller"
        )
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller"
        )
        lease = self.auth.acquire_lease(rec.unique_key, worker_id="w-1", ttl_seconds=60)
        rec = self.auth.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        return spec, rec, lease

    # 1. 省略 epoch 的非法 Worker 寫入
    def test_worker_omitting_epoch_or_execution_id_rejected(self) -> None:
        spec, rec, lease = self._prepare_running("h-no-epoch")
        with self.assertRaises(StaleEpochError):
            self.auth.transition_task(
                spec.unique_key,
                TaskStatus.VERIFYING,
                expected_revision=rec.revision,
                actor_role="worker",
                epoch=None,
                worker_id="w-1",
                execution_id=lease.execution_id,
            )
        with self.assertRaises(PolicyDeniedError):
            self.auth.transition_task(
                spec.unique_key,
                TaskStatus.VERIFYING,
                expected_revision=rec.revision,
                actor_role="controller",
            )

    # 2. 租約過期後完成任務
    def test_expired_lease_cannot_complete_task(self) -> None:
        spec, rec, lease = self._prepare_running("h-exp-complete")
        rec = self.auth.transition_task(
            spec.unique_key,
            TaskStatus.VERIFYING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        self.clock.advance(61)
        with self.assertRaises(LeaseExpiredError):
            self.auth.transition_task(
                spec.unique_key,
                TaskStatus.SUCCEEDED,
                expected_revision=rec.revision,
                actor_role="worker",
                epoch=lease.fencing_epoch,
                worker_id="w-1",
                execution_id=lease.execution_id,
            )

    # 3. PENDING Intent 重啟恢復
    def test_pending_intent_on_restart_enters_reconciliation_not_ready(self) -> None:
        spec, rec, lease = self._prepare_running("h-pending-restart")
        self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"msg": "in-flight"},
            operation_id="op-1",
        )
        self.clock.advance(120)
        restarted_auth = ExecutionAuthority(SQLiteStore(self.db_path, clock=self.clock), clock=self.clock)
        summary = restarted_auth.recover_on_restart()
        self.assertIn(spec.unique_key, summary["moved_to_reconciliation"])
        self.assertNotIn(spec.unique_key, summary["recovered_to_ready"])
        self.assertEqual(restarted_auth.get_task(spec.unique_key).status, TaskStatus.NEEDS_RECONCILIATION)

    # 4. 重複 ToolIntent（確定性冪等鍵）
    def test_deterministic_idempotency_key_deduplicates_same_op_and_allows_distinct_op(self) -> None:
        spec, _, lease = self._prepare_running("h-idem")
        intent1 = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"x": 42},
            operation_id="step-1",
        )
        intent2 = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"x": 42},
            operation_id="step-1",
        )
        self.assertEqual(intent1.intent.intent_id, intent2.intent.intent_id)
        self.assertEqual(intent1.intent.idempotency_key, intent2.intent.idempotency_key)

        # Same operation_id with different args -> conflict
        with self.assertRaises(TaskConflictError):
            self.auth.record_tool_intent(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                tool="mock.echo",
                args={"x": 99},
                operation_id="step-1",
            )

        # Distinct operation_id -> distinct authorized repeat operation
        intent3 = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"x": 42},
            operation_id="step-2",
        )
        self.assertNotEqual(intent1.intent.intent_id, intent3.intent.intent_id)

    # 5. 不存在的 / 跨任務 / 錯誤 execution / 非法 outcome Receipt
    def test_nonexistent_cross_task_wrong_exec_and_conflicting_receipt_rejected(self) -> None:
        spec_a, _, lease_a = self._prepare_running("h-rcpt-a")
        spec_b, _, lease_b = self._prepare_running("h-rcpt-b")
        intent_a = self.auth.record_tool_intent(
            spec_a.unique_key,
            worker_id="w-1",
            execution_id=lease_a.execution_id,
            epoch=lease_a.fencing_epoch,
            tool="mock.echo",
            args={"k": "v"},
        )

        # Non-existent intent
        with self.assertRaises(TaskNotFoundError):
            self.auth.record_effect_receipt(
                spec_a.unique_key,
                EffectReceipt("missing-intent", EffectOutcome.SUCCEEDED, "r", "h"),
                worker_id="w-1",
                epoch=lease_a.fencing_epoch,
                execution_id=lease_a.execution_id,
            )

        # Cross-task intent
        with self.assertRaises(TaskConflictError):
            self.auth.record_effect_receipt(
                spec_b.unique_key,
                EffectReceipt(intent_a.intent.intent_id, EffectOutcome.SUCCEEDED, "r", "h"),
                worker_id="w-1",
                epoch=lease_b.fencing_epoch,
                execution_id=lease_b.execution_id,
            )

        # Revert to PENDING illegal
        with self.assertRaises(InvalidTransitionError):
            self.auth.record_effect_receipt(
                spec_a.unique_key,
                EffectReceipt(intent_a.intent.intent_id, EffectOutcome.PENDING, "r", "h"),
                worker_id="w-1",
                epoch=lease_a.fencing_epoch,
                execution_id=lease_a.execution_id,
            )

        # Settle once as SUCCEEDED, then conflicting FAILED rejected
        self.auth.record_effect_receipt(
            spec_a.unique_key,
            EffectReceipt(intent_a.intent.intent_id, EffectOutcome.SUCCEEDED, "ref-1", "hash-1"),
            worker_id="w-1",
            epoch=lease_a.fencing_epoch,
            execution_id=lease_a.execution_id,
        )
        with self.assertRaises(InvalidTransitionError):
            self.auth.record_effect_receipt(
                spec_a.unique_key,
                EffectReceipt(intent_a.intent.intent_id, EffectOutcome.FAILED, "ref-2", "hash-2"),
                worker_id="w-1",
                epoch=lease_a.fencing_epoch,
                execution_id=lease_a.execution_id,
            )

    # 6. 路徑穿越與絕對路徑
    def test_path_traversal_and_absolute_paths_rejected(self) -> None:
        gate = PolicyGate()
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="r",
            task_id="t-path",
            goal="paths",
            acceptance_hash="h",
            allowed_paths=("src/opencognitiveos",),
            allowed_tools=("mock.echo",),
        )
        for bad in ("/etc/passwd", "../secrets.txt", "src/opencognitiveos/../../etc/shadow", "C:\\Windows\\win.ini"):
            with self.assertRaises(PolicyDeniedError):
                gate.check_tool(spec, "mock.echo", args={"path": bad})

    # 7. 工具參數與授權路徑不一致
    def test_tool_args_and_target_path_mismatch_rejected(self) -> None:
        gate = PolicyGate()
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="r",
            task_id="t-mismatch",
            goal="mismatch",
            acceptance_hash="h",
            allowed_paths=("src/opencognitiveos",),
            allowed_tools=("mock.echo",),
        )
        with self.assertRaises(PolicyDeniedError):
            gate.check_tool(
                spec,
                "mock.echo",
                args={"path": "outside/evil.py"},
                target_path="src/opencognitiveos/ok.py",
            )

    # 8. 未授權雲端（M1 即使 cloud_approved=True 仍拒絕）
    def test_cloud_provider_disabled_in_m1_even_when_spec_approved(self) -> None:
        gate = PolicyGate()
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="r",
            task_id="t-cloud",
            goal="cloud",
            acceptance_hash="h",
            data_class=DataClass.PUBLIC,
            allowed_providers=("cloud-future",),
            cloud_approved=True,
            deadline="2026-12-01T00:00:00Z",
            budget=Budget(max_cost_usd=5.0),
        )
        with self.assertRaises(PolicyDeniedError):
            gate.check_provider(spec, _FakeApprovedCloudProvider())

    # 9. 多 Worker 重試競爭
    def test_concurrent_retry_race_respects_budget_atomically(self) -> None:
        spec, rec, _ = self._prepare_running("h-retry-race", budget=Budget(max_retries=1))
        barrier = threading.Barrier(2)
        successes = 0
        failures = 0
        lock = threading.Lock()

        def try_retry() -> None:
            nonlocal successes, failures
            local_auth = ExecutionAuthority(SQLiteStore(self.db_path, clock=self.clock), clock=self.clock)
            barrier.wait()
            try:
                local_auth.schedule_retry(spec.unique_key, expected_revision=rec.revision, reason="race")
                with lock:
                    successes += 1
            except (CASConflictError, InvalidTransitionError, BudgetExceededError):
                with lock:
                    failures += 1

        t1 = threading.Thread(target=try_retry)
        t2 = threading.Thread(target=try_retry)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(successes, 1)
        self.assertEqual(failures, 1)
        self.assertEqual(self.auth.get_task(spec.unique_key).retry_count, 1)

    # 10. Receipt 與狀態提交中斷
    def test_receipt_and_reconciliation_transition_atomic_rollback(self) -> None:
        spec, rec, lease = self._prepare_running("h-rcpt-crash")
        intent = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"a": 1},
        )

        def crash(_conn) -> None:
            raise RuntimeError("crash before receipt commit")

        with self.assertRaises(RuntimeError):
            self.auth.record_effect_receipt(
                spec.unique_key,
                EffectReceipt(intent.intent.intent_id, EffectOutcome.UNKNOWN, None, "ev-hash"),
                worker_id="w-1",
                epoch=lease.fencing_epoch,
                execution_id=lease.execution_id,
                before_commit_hook=crash,
            )

        # Task must still be RUNNING and effect must still be PENDING
        self.assertEqual(self.auth.get_task(spec.unique_key).status, TaskStatus.RUNNING)
        effs = self.auth.get_effects(spec.unique_key)
        self.assertEqual(effs[0]["outcome"], EffectOutcome.PENDING.value)

    # 11. SQLite Connection 關閉（無 ResourceWarning）
    def test_sqlite_connections_closed_zero_resource_warnings(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            spec, rec, lease = self._prepare_running("h-conn-close")
            self.auth.heartbeat_lease(
                spec.unique_key, worker_id="w-1", epoch=lease.fencing_epoch, execution_id=lease.execution_id
            )
            self.store.integrity_check()
            self.auth.recover_on_restart()
            gc.collect()
        resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])

    # 12. 未經驗收直接完成
    def test_running_cannot_transition_directly_to_succeeded_and_verifier_rejects_empty_effects(self) -> None:
        spec, rec, lease = self._prepare_running("h-no-verify")
        with self.assertRaises(InvalidTransitionError):
            self.auth.transition_task(
                spec.unique_key,
                TaskStatus.SUCCEEDED,
                expected_revision=rec.revision,
                actor_role="worker",
                epoch=lease.fencing_epoch,
                worker_id="w-1",
                execution_id=lease.execution_id,
            )
        verifier = IndependentVerifier()
        with self.assertRaises(VerificationFailedError):
            verifier.verify(spec, effects=[], output_payload={"output_hash": "abc"}, execution_id=lease.execution_id, epoch=lease.fencing_epoch, revision=rec.revision)

    # 13. 多消費者 Outbox（含 Handler 失敗不標記 DISPATCHED）
    def test_multi_consumer_outbox_and_handler_failure_retry(self) -> None:
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="h-multi-outbox",
            goal="multi consumer",
            acceptance_hash="h",
        )
        self.auth.submit_task(spec)

        seen_a: list[str] = []
        seen_b: list[str] = []

        def flaky_handler(_msg: dict) -> None:
            raise RuntimeError("downstream unavailable")

        with self.assertRaises(RuntimeError):
            self.auth.dispatch_outbox("consumer-A", flaky_handler)

        # Must remain PENDING after handler failure
        with self.store.open_connection() as conn:
            row = conn.execute("SELECT status, delivery_attempts, last_error FROM outbox LIMIT 1;").fetchone()
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(int(row["delivery_attempts"]), 1)
        self.assertIn("downstream unavailable", row["last_error"])

        # Retry succeeds for consumer-A, and consumer-B also receives independently
        self.assertEqual(self.auth.dispatch_outbox("consumer-A", lambda m: seen_a.append(m["event_id"])), 1)
        self.assertEqual(
            self.auth.dispatch_outbox("consumer-B", lambda m: seen_b.append(m["event_id"]), include_dispatched=True),
            1,
        )
        self.assertEqual(seen_a, seen_b)

    # 14. VERIFYING 狀態重啟
    def test_verifying_state_restart_recovery(self) -> None:
        spec, rec, lease = self._prepare_running("h-verifying-restart")
        intent = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"ok": True},
        )
        rec = self.auth.record_effect_receipt(
            spec.unique_key,
            EffectReceipt(intent.intent.intent_id, EffectOutcome.SUCCEEDED, "ref-v", "ev-v"),
            worker_id="w-1",
            epoch=lease.fencing_epoch,
            execution_id=lease.execution_id,
        )
        self.auth.transition_task(
            spec.unique_key,
            TaskStatus.VERIFYING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        self.clock.advance(120)
        restarted_auth = ExecutionAuthority(SQLiteStore(self.db_path, clock=self.clock), clock=self.clock)
        summary = restarted_auth.recover_on_restart()
        self.assertIn(spec.unique_key, summary["recovered_to_ready"])
        self.assertEqual(restarted_auth.get_task(spec.unique_key).status, TaskStatus.READY)


if __name__ == "__main__":
    unittest.main()
