"""M0-M1.2 Regression Tests covering VerificationReceipt, path boundaries, cloud disablement, replay, and Runner hardening."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opencognitiveos.contracts.models import (
    Budget,
    DataClass,
    EffectOutcome,
    EffectReceipt,
    InjectableClock,
    IntentDisposition,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.errors import PolicyDeniedError, VerificationFailedError
from opencognitiveos.execution.runner import OfflineRunner
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.kernel.policy import PolicyGate
from opencognitiveos.providers.mock_provider import MockProvider
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.verifier import IndependentVerifier


class _FailingProvider:
    name = "mock-local"
    is_cloud = False

    def capabilities(self) -> dict:
        return {"provider": self.name, "is_cloud": False}

    def infer(self, request: dict) -> dict:
        raise RuntimeError("Simulated provider crash")

    def cancel(self, run_id: str) -> bool:
        return True

    def quota(self) -> dict:
        return {"remaining": "local"}


class _TamperedOutputProvider(MockProvider):
    def infer(self, request: dict) -> dict:
        out = super().infer(request)
        out["proposed_args"]["summary"] = "tampered-self-declaration"
        return out


class TestM12Hardening(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos_m12.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)
        self.verifier = self.auth.verifier

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _prepare_verifying(self, task_id: str) -> tuple[TaskSpec, object, object, dict]:
        goal = f"goal-{task_id}"
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id=task_id,
            goal=goal,
            acceptance_hash=canonical_json_hash({"goal": goal}),
            allowed_paths=("src/opencognitiveos",),
            allowed_tools=("mock.echo",),
        )
        rec, _ = self.auth.submit_task(spec)
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.PLANNED, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
        )
        rec = self.auth.transition_task(
            rec.unique_key, TaskStatus.READY, expected_revision=rec.revision, actor_role="controller", controller_capability=self.auth._controller_capability
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
        proposal = MockProvider().infer({"run_id": lease.execution_id, "goal": goal, "tool": "mock.echo"})
        sub = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args=proposal["proposed_args"],
        )
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
        return spec, rec, lease, proposal

    # 1. 禁止 Worker 透過 VERIFYING -> SUCCEEDED 繞過 IndependentVerifier
    def test_worker_cannot_bypass_verifier_to_succeed(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying("m12-no-bypass")
        with self.assertRaises(VerificationFailedError):
            self.auth.transition_task(
                spec.unique_key,
                TaskStatus.SUCCEEDED,
                expected_revision=rec.revision,
                actor_role="worker",
                epoch=lease.fencing_epoch,
                worker_id="w-1",
                execution_id=lease.execution_id,
            )

        completed, v_receipt = self.auth.verify_and_complete(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            expected_revision=rec.revision,
            verifier=self.verifier,
            output_payload=proposal,
        )
        self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
        persisted = self.auth.get_verification_receipts(spec.unique_key)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].verification_id, v_receipt.verification_id)

    # 1b. 完成狀態與驗收證據原子提交回滾
    def test_verify_and_complete_atomic_rollback_on_crash(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying("m12-atomic-verify")

        def crash(_conn) -> None:
            raise RuntimeError("crash before verify commit")

        with self.assertRaises(RuntimeError):
            self.auth.verify_and_complete(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                expected_revision=rec.revision,
                verifier=self.verifier,
                output_payload=proposal,
                before_commit_hook=crash,
            )

        self.assertEqual(self.auth.get_task(spec.unique_key).status, TaskStatus.VERIFYING)
        self.assertEqual(self.auth.get_verification_receipts(spec.unique_key), [])

    # 2. 路徑正規化禁止 lstrip("./")，拒絕 .src/opencognitiveos/private，保留合法隱藏目錄
    def test_path_normalization_rejects_dot_prefix_spoof_and_allows_hidden_dir(self) -> None:
        gate = PolicyGate()
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="r",
            task_id="m12-path",
            goal="path boundaries",
            acceptance_hash="h",
            allowed_paths=("src/opencognitiveos",),
            allowed_tools=("mock.echo",),
        )
        # Must reject .src/opencognitiveos/private (previously bypassed via lstrip("./"))
        with self.assertRaises(PolicyDeniedError):
            gate.check_tool(spec, "mock.echo", args={"path": ".src/opencognitiveos/private"})

        # Must reject sibling directory with shared prefix
        with self.assertRaises(PolicyDeniedError):
            gate.check_tool(spec, "mock.echo", args={"path": "src/opencognitiveos_other/secret.py"})

        # Must allow legitimate hidden directory inside authorized path and exact ./ prefix
        self.assertTrue(gate.check_tool(spec, "mock.echo", args={"path": "src/opencognitiveos/.hidden/ok.py"}).allowed)
        self.assertTrue(gate.check_tool(spec, "mock.echo", args={"path": "./src/opencognitiveos/ok.py"}).allowed)

    # 3. M1 完全停用真實雲端 Provider，禁止透過 allow_cloud_in_runtime=True 繞過
    def test_cloud_disabled_unconditionally_no_runtime_bypass(self) -> None:
        with self.assertRaises(TypeError):
            PolicyGate(allow_cloud_in_runtime=True)  # type: ignore[call-arg]

    # 4. 驗收器明確條件與可獨立重播驗證
    def test_verifier_explicit_criteria_and_independent_replay(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying("m12-replay")
        effects = self.auth.get_effects(spec.unique_key)

        # Tampered output_hash rejected
        bad_proposal = dict(proposal)
        bad_proposal["output_hash"] = "0" * 64
        with self.assertRaises(VerificationFailedError):
            self.verifier.verify(
                spec,
                effects=effects,
                output_payload=bad_proposal,
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                revision=rec.revision,
            )

        # Valid verification + independent replay
        _, v_receipt = self.auth.verify_and_complete(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            expected_revision=rec.revision,
            verifier=self.verifier,
            output_payload=proposal,
        )
        self.assertTrue(
            IndependentVerifier.replay_verify(v_receipt, spec, effects=effects, output_payload=proposal)
        )
        # Tampered replay fails
        self.assertFalse(
            IndependentVerifier.replay_verify(v_receipt, spec, effects=effects, output_payload=bad_proposal)
        )

    # 5. ToolIntent 重送區分新建、待對帳及已結算狀態，已結算不重執行
    def test_tool_intent_distinguishes_created_pending_and_settled(self) -> None:
        goal = "intent states"
        spec = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="m12-intent-states",
            goal=goal,
            acceptance_hash=canonical_json_hash({"goal": goal}),
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

        # 1) CREATED
        sub1 = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"k": 1},
            operation_id="op-unique",
        )
        self.assertEqual(sub1.disposition, IntentDisposition.CREATED)
        self.assertTrue(sub1.should_execute)

        # 2) PENDING_RECONCILIATION when re-sent while still PENDING
        sub2 = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"k": 1},
            operation_id="op-unique",
        )
        self.assertEqual(sub2.disposition, IntentDisposition.PENDING_RECONCILIATION)
        self.assertFalse(sub2.should_execute)

        # 3) SETTLED once EffectReceipt recorded -> must NOT execute again
        self.auth.record_effect_receipt(
            spec.unique_key,
            EffectReceipt(sub1.intent.intent_id, EffectOutcome.SUCCEEDED, "ext-1", "ev-1"),
            worker_id="w-1",
            epoch=lease.fencing_epoch,
            execution_id=lease.execution_id,
        )
        sub3 = self.auth.record_tool_intent(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool="mock.echo",
            args={"k": 1},
            operation_id="op-unique",
        )
        self.assertEqual(sub3.disposition, IntentDisposition.SETTLED)
        self.assertFalse(sub3.should_execute)

    # 6. Runner 處理 Provider 例外與驗收失敗
    def test_runner_handles_provider_exception_and_verification_failure(self) -> None:
        spec1 = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="m12-runner-prov-err",
            goal="provider fails",
            acceptance_hash=canonical_json_hash({"goal": "provider fails"}),
            budget=Budget(max_retries=0),
        )
        res1 = OfflineRunner(self.auth, _FailingProvider()).run_task(spec1)
        self.assertEqual(res1.status, TaskStatus.FAILED)
        self.assertIn("Provider inference error", res1.error_message or "")

        spec2 = TaskSpec(
            project_id="proj-1",
            repo_id="and14122001/OpenCognitiveOS",
            task_id="m12-runner-ver-err",
            goal="tampered summary",
            acceptance_hash=canonical_json_hash({"goal": "tampered summary"}),
            budget=Budget(max_retries=0),
        )
        res2 = OfflineRunner(self.auth, _TamperedOutputProvider()).run_task(spec2)
        self.assertEqual(res2.status, TaskStatus.FAILED)
        self.assertIn("Independent verification failed", res2.error_message or "")


if __name__ == "__main__":
    unittest.main()
