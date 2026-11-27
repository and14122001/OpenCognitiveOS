"""Tests for state machine transitions, cancel-vs-complete race, retry budget, and PolicyGate."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from opencognitiveos.contracts.models import (
    Budget,
    DataClass,
    InjectableClock,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.errors import (
    BudgetExceededError,
    CASConflictError,
    InvalidTransitionError,
    PolicyDeniedError,
    VerificationFailedError,
)
from opencognitiveos.execution.runner import OfflineRunner
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.contracts.models import EffectOutcome, EffectReceipt
from opencognitiveos.providers.mock_provider import MockProvider, UnauthorizedCloudProvider
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.verifier import IndependentVerifier


class TestStateMachineBudgetAndPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_illegal_transition_and_terminal_immutability(self) -> None:
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-sm",
            goal="state machine",
            acceptance_hash=canonical_json_hash({"goal": "state machine"}),
        )
        rec, _ = self.auth.submit_task(spec)
        with self.assertRaises(InvalidTransitionError):
            self.auth.transition_task(
                rec.unique_key, TaskStatus.SUCCEEDED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
            )

        cancelled = self.auth.cancel_task(rec.unique_key, expected_revision=rec.revision)
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        with self.assertRaises(InvalidTransitionError):
            self.auth.transition_task(
                cancelled.unique_key, TaskStatus.READY, expected_revision=cancelled.revision, actor_role="controller", controller_capability=self.auth._controller_capability
            )

    def test_native_reservations_and_controller_authority(self):
        spec = TaskSpec("p", "r", "native", "test", canonical_json_hash({"goal":"native"}),
                        allowed_tools=("sandbox.python",))
        rec, _ = self.auth.submit_task(spec)
        with self.assertRaises(PolicyDeniedError):
            self.auth.transition_task(spec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision,
                                      actor_role="controller")
        for target in (TaskStatus.PLANNED, TaskStatus.READY):
            rec = self.auth.transition_task(spec.unique_key, target, expected_revision=rec.revision,
                         actor_role="controller", controller_capability=self.auth._controller_capability)
        lease = self.auth.acquire_lease(spec.unique_key, "worker")
        fence = dict(worker_id="worker", execution_id=lease.execution_id, epoch=lease.fencing_epoch)
        self.auth.transition_task(spec.unique_key, TaskStatus.RUNNING, expected_revision=rec.revision, **fence)
        def reserve(number, capability=None):
            return self.auth.record_tool_intent(spec.unique_key, tool="sandbox.python", args={},
                       operation_id=str(number), broker_capability=capability, **fence)
        with self.assertRaises(PolicyDeniedError):
            reserve(0)
        cap = self.auth._broker_capability
        first = reserve(1, cap)
        reserve(2, cap)
        self.assertFalse(reserve(1, cap).should_execute)
        with self.assertRaises(BudgetExceededError):
            reserve(3, cap)
        with self.assertRaises(VerificationFailedError):
            self.auth.record_effect_receipt(spec.unique_key, EffectReceipt(first.intent.intent_id,
                            EffectOutcome.FAILED, "unit", "a"*64), broker_capability=cap, **fence)
        with self.assertRaises(BudgetExceededError):
            reserve(3, cap)

    def test_cancel_and_complete_race_resolved_by_cas(self) -> None:
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-race",
            goal="cancel vs complete",
            acceptance_hash=canonical_json_hash({"goal": "cancel vs complete"}),
        )
        rec, _ = self.auth.submit_task(spec)
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        lease = self.auth.acquire_lease(rec.unique_key, worker_id="w1")
        running = self.auth.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w1",
            execution_id=lease.execution_id,
        )
        proposal = MockProvider().infer({"run_id": lease.execution_id, "goal": "cancel vs complete", "tool": "mock.echo"})
        intent_sub = self.auth.record_tool_intent(
            running.unique_key,
            worker_id="w1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args=proposal["proposed_args"],
        )
        self.auth.record_effect_receipt(
            running.unique_key,
            EffectReceipt(intent_sub.intent.intent_id, EffectOutcome.SUCCEEDED, "ref-race", "ev-race"),
            worker_id="w1",
            epoch=lease.fencing_epoch,
            execution_id=lease.execution_id,
        )
        verifying = self.auth.transition_task(
            running.unique_key,
            TaskStatus.VERIFYING,
            expected_revision=running.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w1",
            execution_id=lease.execution_id,
        )
        proposal = MockProvider().infer({"run_id": lease.execution_id, "goal": "cancel vs complete", "tool": "mock.echo"})

        barrier = threading.Barrier(2)
        winners: list[TaskStatus] = []
        losers: list[Exception] = []
        lock = threading.Lock()

        def do_complete() -> None:
            local_auth = ExecutionAuthority(SQLiteStore(self.db_path, clock=self.clock), clock=self.clock)
            barrier.wait()
            try:
                done, _ = local_auth.verify_and_complete(
                    verifying.unique_key,
                    worker_id="w1",
                    execution_id=lease.execution_id,
                    epoch=lease.fencing_epoch,
                    expected_revision=verifying.revision,
                    verifier=local_auth.verifier,
                    output_payload=proposal,
                )
                with lock:
                    winners.append(done.status)
            except (CASConflictError, InvalidTransitionError) as exc:
                with lock:
                    losers.append(exc)

        def do_cancel() -> None:
            local_auth = ExecutionAuthority(SQLiteStore(self.db_path, clock=self.clock), clock=self.clock)
            barrier.wait()
            try:
                done = local_auth.cancel_task(verifying.unique_key, expected_revision=verifying.revision)
                with lock:
                    winners.append(done.status)
            except (CASConflictError, InvalidTransitionError) as exc:
                with lock:
                    losers.append(exc)

        t1 = threading.Thread(target=do_complete)
        t2 = threading.Thread(target=do_cancel)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn(winners[0], {TaskStatus.SUCCEEDED, TaskStatus.CANCELLED})

    def test_retry_preserves_budget_and_permissions(self) -> None:
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-retry",
            goal="finite retry",
            acceptance_hash=canonical_json_hash({"goal": "finite retry"}),
            budget=Budget(max_retries=1, max_cost_usd=0.0),
        )
        rec, _ = self.auth.submit_task(spec)
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        lease = self.auth.acquire_lease(rec.unique_key, worker_id="w1")
        rec = self.auth.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w1",
            execution_id=lease.execution_id,
        )

        rec = self.auth.schedule_retry(rec.unique_key, expected_revision=rec.revision, reason="transient")
        self.assertEqual(rec.status, TaskStatus.READY)
        self.assertEqual(rec.retry_count, 1)
        self.assertEqual(rec.spec.budget.max_cost_usd, 0.0)
        self.assertFalse(rec.spec.cloud_approved)

        with self.assertRaises(InvalidTransitionError):
            self.auth.schedule_retry(rec.unique_key, expected_revision=rec.revision, reason="not running")

        # Lease again and exhaust retry_count=1
        lease2 = self.auth.acquire_lease(rec.unique_key, worker_id="w1")
        rec = self.auth.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease2.fencing_epoch,
            worker_id="w1",
            execution_id=lease2.execution_id,
        )
        with self.assertRaises(BudgetExceededError):
            self.auth.schedule_retry(rec.unique_key, expected_revision=rec.revision, reason="second failure")

    def test_policy_gate_denies_unauthorized_cloud_and_tools(self) -> None:
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="t-policy",
            goal="policy enforcement",
            acceptance_hash=canonical_json_hash({"goal": "policy enforcement"}),
            allowed_paths=("src/opencognitiveos",),
            data_class=DataClass.RESTRICTED,
            allowed_providers=("mock-local",),
            allowed_tools=("mock.echo",),
            cloud_approved=False,
        )
        cloud_runner = OfflineRunner(self.auth, UnauthorizedCloudProvider())
        with self.assertRaises(PolicyDeniedError):
            cloud_runner.run_task(spec)

        mock_runner = OfflineRunner(self.auth, MockProvider())
        with self.assertRaises(PolicyDeniedError):
            mock_runner.run_task(
                TaskSpec(
                    project_id="proj-1",
                    repo_id="and14122001/OpenCognitiveOS",
                    task_id="t-bad-tool",
                    goal="bad tool",
                    acceptance_hash="h",
                    allowed_tools=("mock.echo",),
                ),
                tool="shell.rm_rf",
            )

        with self.assertRaises(PolicyDeniedError):
            mock_runner.run_task(
                TaskSpec(
                    project_id="proj-1",
                    repo_id="and14122001/OpenCognitiveOS",
                    task_id="t-bad-path",
                    goal="bad path",
                    acceptance_hash="h",
                    allowed_paths=("src/opencognitiveos",),
                    allowed_tools=("mock.echo",),
                ),
                tool="mock.echo",
                target_path="etc/passwd",
            )


if __name__ == "__main__":
    unittest.main()
