"""M0-M1.3 Security Tests covering P0-01 (failed/forged VerificationReceipt, verifier substitution) and P0-02 (full-payload replay, retry isolation, lease expiry during VERIFYING)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from opencognitiveos import __version__
from opencognitiveos.contracts.models import (
    Budget,
    EffectOutcome,
    EffectReceipt,
    InjectableClock,
    TaskSpec,
    TaskStatus,
    VerificationReceipt,
    canonical_json_hash,
)
from opencognitiveos.errors import (
    CASConflictError,
    InvalidTransitionError,
    LeaseExpiredError,
    PolicyDeniedError,
    VerificationFailedError,
)
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.providers.mock_provider import MockProvider
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.verifier import IndependentVerifier


class _FakePassingFalseVerifier(IndependentVerifier):
    def verify(self, spec, *, effects, output_payload, execution_id, epoch, revision) -> VerificationReceipt:
        receipt = super().verify(
            spec, effects=effects, output_payload=output_payload, execution_id=execution_id, epoch=epoch, revision=revision
        )
        return VerificationReceipt(
            verification_id=receipt.verification_id,
            task_unique_key=receipt.task_unique_key,
            acceptance_hash=receipt.acceptance_hash,
            execution_id=receipt.execution_id,
            epoch=receipt.epoch,
            revision=receipt.revision,
            passed=False,
            evidence_hash=receipt.evidence_hash,
            evidence_json=receipt.evidence_json,
            verifier_name=receipt.verifier_name,
            verified_at_utc=receipt.verified_at_utc,
        )


class _ForgedFieldVerifier(IndependentVerifier):
    def __init__(self, clock, *, field_to_forge: str) -> None:
        super().__init__(clock=clock)
        self.field_to_forge = field_to_forge

    def verify(self, spec, *, effects, output_payload, execution_id, epoch, revision) -> VerificationReceipt:
        r = super().verify(
            spec, effects=effects, output_payload=output_payload, execution_id=execution_id, epoch=epoch, revision=revision
        )
        kwargs = {
            "verification_id": r.verification_id,
            "task_unique_key": r.task_unique_key,
            "acceptance_hash": r.acceptance_hash,
            "execution_id": r.execution_id,
            "epoch": r.epoch,
            "revision": r.revision,
            "passed": r.passed,
            "evidence_hash": r.evidence_hash,
            "evidence_json": r.evidence_json,
            "verifier_name": r.verifier_name,
            "verified_at_utc": r.verified_at_utc,
        }
        if self.field_to_forge == "execution_id":
            kwargs["execution_id"] = "forged-exec-id"
        elif self.field_to_forge == "epoch":
            kwargs["epoch"] = epoch + 99
        elif self.field_to_forge == "revision":
            kwargs["revision"] = revision + 99
        elif self.field_to_forge == "acceptance_hash":
            kwargs["acceptance_hash"] = "forged-acceptance"
        elif self.field_to_forge == "evidence_hash":
            kwargs["evidence_hash"] = "0" * 64
        return VerificationReceipt(**kwargs)


class TestM13Security(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ocos_m13.db"
        self.clock = InjectableClock()
        self.store = SQLiteStore(self.db_path, clock=self.clock)
        self.store.migrate()
        self.auth = ExecutionAuthority(self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _prepare_verifying(self, task_id: str, **spec_overrides) -> tuple[TaskSpec, object, object, dict]:
        goal = f"goal-{task_id}"
        kwargs = {
            "project_id": "proj-1",
            "repo_id": "and14122001/OpenCognitiveOS",
            "task_id": task_id,
            "goal": goal,
            "acceptance_hash": canonical_json_hash({"goal": goal}),
            "allowed_paths": ("src/opencognitiveos",),
            "allowed_tools": ("mock.echo",),
        }
        kwargs.update(spec_overrides)
        spec = TaskSpec(**kwargs)
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

    # 1. P0-01: Reject passed=False VerificationReceipt
    def test_reject_passed_false_verification_receipt(self) -> None:
        bad_auth = ExecutionAuthority(
            self.store, clock=self.clock, verifier=_FakePassingFalseVerifier(clock=self.clock)
        )
        spec, rec, lease, proposal = self._prepare_verifying("m13-passed-false")
        with self.assertRaises(VerificationFailedError):
            bad_auth.verify_and_complete(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                expected_revision=rec.revision,
                output_payload=proposal,
            )
        self.assertEqual(self.auth.get_task(spec.unique_key).status, TaskStatus.VERIFYING)
        self.assertEqual(self.auth.get_verification_receipts(spec.unique_key), [])

    # 2. P0-01: Reject forged fields in VerificationReceipt
    def test_reject_forged_verification_receipt_fields(self) -> None:
        for idx, field_name in enumerate(("execution_id", "epoch", "revision", "acceptance_hash", "evidence_hash")):
            forged_auth = ExecutionAuthority(
                self.store, clock=self.clock, verifier=_ForgedFieldVerifier(self.clock, field_to_forge=field_name)
            )
            spec, rec, lease, proposal = self._prepare_verifying(f"m13-forged-{idx}")
            with self.assertRaises(VerificationFailedError):
                forged_auth.verify_and_complete(
                    spec.unique_key,
                    worker_id="w-1",
                    execution_id=lease.execution_id,
                    epoch=lease.fencing_epoch,
                    expected_revision=rec.revision,
                    output_payload=proposal,
                )
            self.assertEqual(self.auth.get_task(spec.unique_key).status, TaskStatus.VERIFYING)

    # 3. Worker cannot substitute controller-managed verifier
    def test_worker_cannot_substitute_controller_verifier(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying("m13-subst-verifier")
        rogue_verifier = IndependentVerifier(clock=self.clock)
        with self.assertRaises(PolicyDeniedError):
            self.auth.verify_and_complete(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                expected_revision=rec.revision,
                verifier=rogue_verifier,
                output_payload=proposal,
            )

    # 4. P0-02: Tampering output_payload.proposed_args.summary causes replay_verify() to return False
    def test_tampered_output_summary_fails_replay_verify(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying("m13-tamper-summary")
        effects = self.auth.get_effects(spec.unique_key)
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

        # Tamper summary ONLY while keeping output_hash intact -> MUST fail replay_verify!
        tampered = {
            **proposal,
            "proposed_args": {**proposal["proposed_args"], "summary": "TAMPERED-SUMMARY"},
        }
        self.assertFalse(
            IndependentVerifier.replay_verify(v_receipt, spec, effects=effects, output_payload=tampered)
        )

    # 5. Persisted replay_verification from SQLite without Worker re-supplying data
    def test_persisted_replay_verification_from_sqlite_and_tamper_detection(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying("m13-db-replay")
        self.auth.verify_and_complete(
            spec.unique_key,
            worker_id="w-1",
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            expected_revision=rec.revision,
            output_payload=proposal,
        )
        self.assertTrue(self.auth.replay_verification(spec.unique_key))

        # Tamper persisted output_payload_json directly in SQLite -> replay_verification detects it
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE verification_receipts SET output_payload_json = ? WHERE task_unique_key = ?;",
                ('{"proposed_tool":"mock.echo","proposed_args":{"goal":"x","summary":"bad"},"output_hash":"0"}', spec.unique_key),
            )
        self.assertFalse(self.auth.replay_verification(spec.unique_key))

    # 6. Lease expiry during VERIFYING + Cancel race + Retry execution isolation
    def test_lease_expiry_during_verifying_and_retry_execution_isolation(self) -> None:
        spec, rec, lease, proposal = self._prepare_verifying("m13-lease-exp-verifying", budget=Budget(max_retries=2))
        self.clock.advance(61)
        with self.assertRaises(LeaseExpiredError):
            self.auth.verify_and_complete(
                spec.unique_key,
                worker_id="w-1",
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                expected_revision=rec.revision,
                output_payload=proposal,
            )

        # Retry task: old execution's effect must NOT be mixed into new execution's verification
        retried = self.auth.schedule_retry(spec.unique_key, expected_revision=rec.revision, reason="lease expired in verifying")
        lease2 = self.auth.acquire_lease(spec.unique_key, worker_id="w-2", ttl_seconds=60)
        running2 = self.auth.transition_task(
            spec.unique_key,
            TaskStatus.RUNNING,
            expected_revision=retried.revision,
            actor_role="worker",
            epoch=lease2.fencing_epoch,
            worker_id="w-2",
            execution_id=lease2.execution_id,
        )
        verifying2 = self.auth.transition_task(
            spec.unique_key,
            TaskStatus.VERIFYING,
            expected_revision=running2.revision,
            actor_role="worker",
            epoch=lease2.fencing_epoch,
            worker_id="w-2",
            execution_id=lease2.execution_id,
        )
        # New execution has zero effects of its own -> verify_and_complete MUST reject!
        with self.assertRaises(VerificationFailedError):
            self.auth.verify_and_complete(
                spec.unique_key,
                worker_id="w-2",
                execution_id=lease2.execution_id,
                epoch=lease2.fencing_epoch,
                expected_revision=verifying2.revision,
                output_payload=proposal,
            )

    def test_version_alignment(self) -> None:
        self.assertEqual(__version__, "0.1.6.dev1")


if __name__ == "__main__":
    unittest.main()
