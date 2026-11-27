"""End-to-end offline task runner with hardened error, cancellation, and verification handling."""

from __future__ import annotations

from typing import Any

from opencognitiveos.contracts.models import (
    EffectOutcome,
    EffectReceipt,
    IntentDisposition,
    Provider,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.errors import (
    BudgetExceededError,
    CASConflictError,
    InvalidTransitionError,
    LeaseConflictError,
    LeaseExpiredError,
    PolicyDeniedError,
    StaleEpochError,
    VerificationFailedError,
)
from opencognitiveos.kernel.authority import ExecutionAuthority, TaskRecord
from opencognitiveos.verification.verifier import IndependentVerifier


class OfflineRunner:
    """Executes a TaskSpec offline through PolicyGate, ExecutionAuthority, and IndependentVerifier."""

    def __init__(
        self,
        authority: ExecutionAuthority,
        provider: Provider,
        verifier: IndependentVerifier | None = None,
    ) -> None:
        self.authority = authority
        self.provider = provider
        self.verifier = verifier or authority.verifier

    def _fail_or_retry(self, unique_key: str, *, revision: int, reason: str) -> TaskRecord:
        try:
            return self.authority.schedule_retry(unique_key, expected_revision=revision, reason=reason)
        except BudgetExceededError:
            latest = self.authority.get_task(unique_key)
            return self.authority.transition_task(
                unique_key,
                TaskStatus.FAILED,
                expected_revision=latest.revision,
                actor_role="controller",
                error_message=reason,
            )

    def run_task(
        self,
        spec: TaskSpec,
        *,
        worker_id: str = "local-worker-1",
        tool: str = "mock.echo",
        target_path: str | None = None,
        operation_id: str = "step-1",
        simulate_lost_response: bool = False,
    ) -> TaskRecord:
        record, _ = self.authority.submit_task(spec)
        if record.status == TaskStatus.CANCELLED:
            return record

        if record.status == TaskStatus.SUBMITTED:
            record = self.authority.transition_task(
                record.unique_key,
                TaskStatus.PLANNED,
                expected_revision=record.revision,
                actor_role="controller",
            )
        if record.status == TaskStatus.PLANNED:
            record = self.authority.transition_task(
                record.unique_key,
                TaskStatus.READY,
                expected_revision=record.revision,
                actor_role="controller",
            )

        self.authority.policy_gate.check_provider(spec, self.provider)

        try:
            lease = self.authority.acquire_lease(record.unique_key, worker_id=worker_id)
        except LeaseConflictError:
            raise

        try:
            record = self.authority.transition_task(
                record.unique_key,
                TaskStatus.RUNNING,
                expected_revision=record.revision,
                actor_role="worker",
                epoch=lease.fencing_epoch,
                worker_id=worker_id,
                execution_id=lease.execution_id,
            )
        except (CASConflictError, InvalidTransitionError):
            current = self.authority.get_task(record.unique_key)
            if current.status == TaskStatus.CANCELLED:
                return current
            raise

        try:
            proposal = self.provider.infer(
                {
                    "run_id": lease.execution_id,
                    "goal": spec.goal,
                    "tool": tool,
                    "target_path": target_path,
                }
            )
            if not isinstance(proposal, dict) or "proposed_tool" not in proposal or not isinstance(
                proposal.get("proposed_args"), dict
            ):
                raise ValueError("Malformed provider output: missing proposed_tool or proposed_args dict.")
        except PolicyDeniedError:
            raise
        except Exception as exc:
            latest = self.authority.get_task(record.unique_key)
            if latest.status == TaskStatus.CANCELLED:
                return latest
            return self._fail_or_retry(
                record.unique_key,
                revision=latest.revision,
                reason=f"Provider inference error: {exc}",
            )

        proposed_tool = str(proposal["proposed_tool"])
        proposed_args: dict[str, Any] = dict(proposal["proposed_args"])
        if target_path is not None:
            proposed_args["target_path"] = target_path
            proposal = {**proposal, "proposed_args": proposed_args}

        intent_sub = self.authority.record_tool_intent(
            record.unique_key,
            worker_id=worker_id,
            execution_id=lease.execution_id,
            epoch=lease.fencing_epoch,
            tool=proposed_tool,
            args=proposed_args,
            operation_id=operation_id,
            target_path=target_path,
        )

        if intent_sub.disposition == IntentDisposition.PENDING_RECONCILIATION:
            latest = self.authority.get_task(record.unique_key)
            return self.authority.transition_task(
                record.unique_key,
                TaskStatus.NEEDS_RECONCILIATION,
                expected_revision=latest.revision,
                actor_role="worker",
                epoch=lease.fencing_epoch,
                worker_id=worker_id,
                execution_id=lease.execution_id,
                error_message=f"Intent '{intent_sub.intent.intent_id}' is already PENDING_RECONCILIATION.",
            )

        if intent_sub.disposition == IntentDisposition.SETTLED and intent_sub.outcome == EffectOutcome.FAILED:
            latest = self.authority.get_task(record.unique_key)
            return self._fail_or_retry(
                record.unique_key,
                revision=latest.revision,
                reason=f"Previously settled effect '{intent_sub.intent.intent_id}' failed.",
            )

        if intent_sub.should_execute:
            if simulate_lost_response:
                receipt = EffectReceipt(
                    intent_id=intent_sub.intent.intent_id,
                    outcome=EffectOutcome.UNKNOWN,
                    external_ref=None,
                    evidence_hash=canonical_json_hash({"status": "response_lost", "intent_id": intent_sub.intent.intent_id}),
                )
                return self.authority.record_effect_receipt(
                    record.unique_key,
                    receipt,
                    worker_id=worker_id,
                    epoch=lease.fencing_epoch,
                    execution_id=lease.execution_id,
                )

            receipt = EffectReceipt(
                intent_id=intent_sub.intent.intent_id,
                outcome=EffectOutcome.SUCCEEDED,
                external_ref=f"mock-ref://{intent_sub.intent.intent_id}",
                evidence_hash=canonical_json_hash({"proposal": proposal, "intent_id": intent_sub.intent.intent_id}),
            )
            record = self.authority.record_effect_receipt(
                record.unique_key,
                receipt,
                worker_id=worker_id,
                epoch=lease.fencing_epoch,
                execution_id=lease.execution_id,
            )

        try:
            record = self.authority.transition_task(
                record.unique_key,
                TaskStatus.VERIFYING,
                expected_revision=record.revision,
                actor_role="worker",
                epoch=lease.fencing_epoch,
                worker_id=worker_id,
                execution_id=lease.execution_id,
            )
            completed, _ = self.authority.verify_and_complete(
                record.unique_key,
                worker_id=worker_id,
                execution_id=lease.execution_id,
                epoch=lease.fencing_epoch,
                expected_revision=record.revision,
                verifier=self.verifier,
                output_payload=proposal,
            )
            return completed
        except VerificationFailedError as exc:
            latest = self.authority.get_task(record.unique_key)
            if latest.status == TaskStatus.CANCELLED:
                return latest
            return self._fail_or_retry(
                record.unique_key,
                revision=latest.revision,
                reason=f"Independent verification failed: {exc}",
            )
        except (LeaseExpiredError, StaleEpochError, BudgetExceededError):
            raise
        except (CASConflictError, InvalidTransitionError):
            latest = self.authority.get_task(record.unique_key)
            if latest.status == TaskStatus.CANCELLED:
                return latest
            raise
