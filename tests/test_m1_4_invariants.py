"""M0-M1.4 Invariant & Fault-Injection Tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opencognitiveos.contracts.models import (
    Budget,
    EffectOutcome,
    EffectReceipt,
    InjectableClock,
    IntentDisposition,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.errors import (
    BudgetExceededError,
    LeaseExpiredError,
    TaskConflictError,
    VerificationFailedError,
)
from opencognitiveos.execution.runner import OfflineRunner
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.providers.mock_provider import MockProvider
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.verifier import IndependentVerifier


class _ClockAdvancingVerifier(IndependentVerifier):
    def __init__(self, clock: InjectableClock, advance_seconds: float) -> None:
        super().__init__(clock=clock)
        self._advance_seconds = advance_seconds

    def verify(self, spec, *, effects, output_payload, execution_id, epoch, revision):
        receipt = super().verify(
            spec, effects=effects, output_payload=output_payload, execution_id=execution_id, epoch=epoch, revision=revision
        )
        # Simulate time elapsing during verification so lease expires before final commit!
        self.clock.advance(self._advance_seconds)
        return receipt


class _MalformedProvider:
    name = "mock-local"
    is_cloud = False

    def capabilities(self) -> dict:
        return {"provider": self.name, "is_cloud": False}

    def infer(self, request: dict) -> dict:
        return {"unexpected": "shape"}  # missing proposed_tool & proposed_args

    def cancel(self, run_id: str) -> bool:
        return True

    def quota(self) -> dict:
        return {"remaining": "local"}


class TestM14Invariants(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos_m14.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _prepare_verifying(self, auth: ExecutionAuthority, task_id: str, **spec_overrides):
        goal = f"goal-{task_id}"
        kwargs = {
            "project_id": "proj-1",
            "repo_id": "and14122001/OpenCognitiveOS",
            "task_id": task_id,
            "goal": goal,
            "acceptance_hash": canonical_json_hash({"goal": goal}),
            "allowed_paths": ("src/opencognitiveos",),
            "allowed_tools": ("mock.echo", "mock.write_note"),
        }
        kwargs.update(spec_overrides)
        spec = TaskSpec(**kwargs)
        rec, _ = auth.submit_task(spec)
        rec = auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=auth._controller_capability
        )
        rec = auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=auth._controller_capability
        )
        lease = auth.acquire_lease(rec.unique_key, worker_id="w-1", ttl_seconds=60)
        rec = auth.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        proposal = MockProvider().infer({"run_id": lease.execution_id, "goal": goal, "tool": "mock.echo"})
        sub = auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args=proposal["proposed_args"],
        )
        rec = auth.record_effect_receipt(
            spec.unique_key,
            EffectReceipt(sub.intent.intent_id, EffectOutcome.SUCCEEDED, "mock-ref://1", "ev-1"),
            worker_id="w-1",
            epoch=lease.fencing_epoch,
            execution_id=lease.execution_id,
        )
        rec = auth.transition_task(
            spec.unique_key,
            TaskStatus.VERIFYING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        return spec, rec, lease, proposal

    # 1. Lease expires DURING verifier.verify() -> final pre-commit re-validation rejects SUCCEEDED
    def test_lease_expires_during_verifier_execution_blocks_commit(self) -> None:
        adv_verifier = _ClockAdvancingVerifier(self.clock, advance_seconds=65)
        auth = ExecutionAuthority(self.store, clock=self.clock, verifier=adv_verifier)
        spec, rec, lease, proposal = self._prepare_verifying(auth, "m14-exp-during-verify")

        with self.assertRaises(LeaseExpiredError):
            auth.verify_and_complete(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                expected_revision=rec.revision,
                output_payload=proposal,
            )
        self.assertEqual(auth.get_task(spec.unique_key).status, TaskStatus.VERIFYING)
        self.assertEqual(auth.get_verification_receipts(spec.unique_key), [])

    # 2. output_payload.run_id / provider mismatch rejected
    def test_output_payload_run_id_and_provider_mismatch_rejected(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying(self.auth, "m14-run-id-mismatch")

        bad_run = {**proposal, "run_id": "wrong-exec-id"}
        with self.assertRaises(VerificationFailedError):
            self.auth.verify_and_complete(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                expected_revision=rec.revision,
                output_payload=bad_run,
            )

        bad_prov = {**proposal, "provider": "rogue-provider"}
        with self.assertRaises(VerificationFailedError):
            self.auth.verify_and_complete(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                expected_revision=rec.revision,
                output_payload=bad_prov,
            )

    # 3. Modifying Effect external_ref or args_hash fails replay_verify
    def test_modifying_effect_external_ref_or_args_hash_fails_replay_verify(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying(self.auth, "m14-eff-tamper")
        effects = self.auth.get_effects(spec.unique_key, execution_id=lease.execution_id)
        _, v_receipt = self.auth.verify_and_complete(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            expected_revision=rec.revision,
            output_payload=proposal,
        )
        self.assertTrue(
            IndependentVerifier.replay_verify(v_receipt, spec, effects=effects, output_payload=proposal)
        )

        # Tamper external_ref
        tampered_ref = [{**effects[0], "external_ref": "mock-ref://HIJACKED"}]
        self.assertFalse(
            IndependentVerifier.replay_verify(v_receipt, spec, effects=tampered_ref, output_payload=proposal)
        )

        # Tamper args_hash
        tampered_args = [{**effects[0], "args_hash": "f" * 64}]
        self.assertFalse(
            IndependentVerifier.replay_verify(v_receipt, spec, effects=tampered_args, output_payload=proposal)
        )

    # 4. max_steps and deadline strictly enforced
    def test_max_steps_and_deadline_enforced(self) -> None:
        # max_steps = 1
        spec_steps = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="m14-max-steps",
            goal="step limit",
            acceptance_hash=canonical_json_hash({"goal": "step limit"}),
            budget=Budget(max_steps=1),
        )
        rec, _ = self.auth.submit_task(spec_steps)
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        lease = self.auth.acquire_lease(rec.unique_key, worker_id="w-1")
        self.auth.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        self.auth.record_tool_intent(
            spec_steps.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"s": 1},
            operation_id="op-1",
        )
        with self.assertRaises(BudgetExceededError):
            self.auth.record_tool_intent(
                spec_steps.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                tool="mock.echo",
                args={"s": 2},
                operation_id="op-2",
            )

        # deadline exceeded
        spec_dl = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="m14-deadline",
            goal="deadline",
            acceptance_hash=canonical_json_hash({"goal": "deadline"}),
            deadline="2026-11-27T00:00:30Z",
        )
        self.auth.submit_task(spec_dl)
        self.clock.advance(40)
        with self.assertRaises(BudgetExceededError):
            self.auth.acquire_lease(spec_dl.unique_key, worker_id="w-1")

    # 5. Idempotency key cannot be bypassed by changing tool on same operation_id
    def test_idempotency_key_cannot_be_bypassed_by_changing_tool(self) -> None:
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="m14-idem-tool",
            goal="tool swap",
            acceptance_hash=canonical_json_hash({"goal": "tool swap"}),
            allowed_tools=("mock.echo", "mock.write_note"),
        )
        rec, _ = self.auth.submit_task(spec)
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        lease = self.auth.acquire_lease(rec.unique_key, worker_id="w-1")
        self.auth.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease.fencing_epoch,
            worker_id="w-1",
            execution_id=lease.execution_id,
        )
        self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"x": 1},
            operation_id="op-same",
        )
        with self.assertRaises(TaskConflictError):
            self.auth.record_tool_intent(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                tool="mock.write_note",
                args={"x": 1},
                operation_id="op-same",
            )

    # 6. Settled effect reused across retry execution without re-execution and completes cleanly
    def test_settled_effect_reused_across_retry_without_reexecution_and_completes(self) -> None:
        goal = "cross retry settled reuse"
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="m14-cross-retry",
            goal=goal,
            acceptance_hash=canonical_json_hash({"goal": goal}),
            budget=Budget(max_retries=2),
        )
        rec, _ = self.auth.submit_task(spec)
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        lease1 = self.auth.acquire_lease(rec.unique_key, worker_id="w-1")
        rec = self.auth.transition_task(
            rec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=rec.revision,
            actor_role="worker",
            epoch=lease1.fencing_epoch,
            worker_id="w-1",
            execution_id=lease1.execution_id,
        )
        proposal1 = MockProvider().infer({"run_id": lease1.execution_id, "goal": goal, "tool": "mock.echo"})
        sub1 = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease1.execution_id,
            epoch=lease1.fencing_epoch,
            tool="mock.echo",
            args=proposal1["proposed_args"],
            operation_id="step-1",
        )
        self.auth.record_effect_receipt(
            spec.unique_key,
            EffectReceipt(sub1.intent.intent_id, EffectOutcome.SUCCEEDED, "ext-once", "ev-once"),
            worker_id="w-1",
            epoch=lease1.fencing_epoch,
            execution_id=lease1.execution_id,
        )
        # Execution 1 fails/retries before completion
        self.auth.schedule_retry(spec.unique_key, expected_revision=self.auth.get_task(spec.unique_key).revision, reason="retry")

        # Execution 2 via OfflineRunner reuses settled effect (does NOT create a 2nd effect) and SUCCEEDS!
        completed = OfflineRunner(self.auth, MockProvider()).run_task(spec, operation_id="step-1")
        self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
        self.assertEqual(len(self.auth.get_effects(spec.unique_key)), 1)
        self.assertTrue(self.auth.replay_verification(spec.unique_key))

    # 7. Malformed provider output handled cleanly
    def test_malformed_provider_output_handled_cleanly(self) -> None:
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="m14-malformed",
            goal="malformed",
            acceptance_hash=canonical_json_hash({"goal": "malformed"}),
            budget=Budget(max_retries=0),
        )
        res = OfflineRunner(self.auth, _MalformedProvider()).run_task(spec)
        self.assertEqual(res.status, TaskStatus.FAILED)
        self.assertIn("Malformed provider output", res.error_message or "")


if __name__ == "__main__":
    unittest.main()
