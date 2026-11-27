"""Independent task acceptance verifier with versioned Effect evidence and full-payload replay."""

from __future__ import annotations

import json
import uuid
from typing import Any

from opencognitiveos.contracts.models import (
    Clock,
    EffectOutcome,
    SystemClock,
    TaskSpec,
    VerificationReceipt,
    canonical_json_hash,
)
from opencognitiveos.errors import VerificationFailedError

EFFECT_EVIDENCE_SCHEMA_VERSION = "1.0.0"


class IndependentVerifier:
    """Controller-managed verifier; never trusts Worker self-declaration."""

    name: str = "independent-mock-verifier-v1.4"

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or SystemClock()

    @staticmethod
    def _canonical_effect_snapshot(eff: dict[str, Any]) -> dict[str, Any]:
        return {
            "intent_id": eff["intent_id"],
            "task_unique_key": eff["task_unique_key"],
            "execution_id": eff["execution_id"],
            "epoch": int(eff["epoch"]),
            "tool": eff["tool"],
            "args_hash": eff["args_hash"],
            "approval_ref": eff["approval_ref"],
            "idempotency_key": eff["idempotency_key"],
            "outcome": eff["outcome"],
            "external_ref": eff["external_ref"],
            "evidence_hash": eff["evidence_hash"],
        }

    @staticmethod
    def _validate_acceptance_criteria(
        spec: TaskSpec,
        *,
        effects: list[dict[str, Any]],
        output_payload: dict[str, Any] | None,
        execution_id: str,
        epoch: int,
    ) -> dict[str, Any]:
        if not spec.acceptance_hash:
            raise VerificationFailedError("TaskSpec.acceptance_hash is empty.")

        if isinstance(output_payload, dict) and isinstance(output_payload.get("verified_evidence"), dict):
            binding = output_payload["verified_evidence"]
            if "worker_log" in binding:
                if (set(output_payload) != {"output_hash", "verified_evidence"} or not binding["worker_log"]
                        or output_payload["output_hash"] != binding["worker_log"] or len(effects) != 1):
                    raise VerificationFailedError("Invalid native output binding.")
                effect = effects[0]
                if (effect["task_unique_key"] != spec.unique_key or effect["execution_id"] != execution_id
                        or effect["epoch"] != epoch or effect["outcome"] != "SUCCEEDED"
                        or effect["tool"] != "sandbox.python" or effect["tool"] not in spec.allowed_tools
                        or effect["approval_ref"] != binding["spec_hash"] or effect["evidence_hash"] != binding["worker_log"]):
                    raise VerificationFailedError("Native execution and pinned evidence differ.")
                return output_payload

        expected_acceptance = canonical_json_hash({"goal": spec.goal})
        if spec.acceptance_hash != expected_acceptance:
            raise VerificationFailedError(
                f"Acceptance hash mismatch: expected {expected_acceptance}, got {spec.acceptance_hash}."
            )

        if not effects:
            raise VerificationFailedError("Cannot verify task with zero recorded ToolIntent/Effect receipts.")

        if not isinstance(output_payload, dict) or not output_payload:
            raise VerificationFailedError("Verification requires non-empty output_payload dict.")

        # Bind output identity & provider to current execution and TaskSpec
        if output_payload.get("run_id") != execution_id:
            raise VerificationFailedError(
                f"output_payload.run_id mismatch: expected '{execution_id}', got '{output_payload.get('run_id')}'."
            )
        if output_payload.get("provider") not in spec.allowed_providers:
            raise VerificationFailedError(
                f"output_payload.provider '{output_payload.get('provider')}' is not in allowed_providers."
            )

        proposed_tool = output_payload.get("proposed_tool")
        proposed_args = output_payload.get("proposed_args")
        if proposed_tool not in spec.allowed_tools:
            raise VerificationFailedError(f"Output tool '{proposed_tool}' is not in allowed_tools.")
        if not isinstance(proposed_args, dict):
            raise VerificationFailedError("output_payload.proposed_args must be a dict.")
        if proposed_args.get("goal") != spec.goal or proposed_args.get("summary") != f"mock-executed:{spec.goal}":
            raise VerificationFailedError("Mock execution output does not satisfy explicit goal/summary criteria.")

        recomputed_output_hash = canonical_json_hash({"goal": spec.goal, "tool": proposed_tool})
        if output_payload.get("output_hash") != recomputed_output_hash:
            raise VerificationFailedError(
                f"Output hash mismatch: expected {recomputed_output_hash}, got {output_payload.get('output_hash')}."
            )

        expected_args_hash = canonical_json_hash(proposed_args)
        matched_authorized_intent = False

        for eff in effects:
            if eff.get("task_unique_key") != spec.unique_key:
                raise VerificationFailedError("Effect task_unique_key mismatch during verification.")
            reused_for = eff.get("reused_for_execution_id")
            if reused_for != execution_id:
                if eff.get("execution_id") != execution_id or int(eff.get("epoch", -1)) != epoch:
                    raise VerificationFailedError("Effect execution_id/epoch does not match current execution.")
            if eff.get("outcome") != EffectOutcome.SUCCEEDED.value:
                raise VerificationFailedError(
                    f"Effect '{eff.get('intent_id')}' has non-succeeded outcome '{eff.get('outcome')}'."
                )
            for req_field in ("tool", "args_hash", "approval_ref", "idempotency_key", "external_ref", "evidence_hash"):
                if not eff.get(req_field):
                    raise VerificationFailedError(f"Effect '{eff.get('intent_id')}' lacks required field '{req_field}'.")
            if eff.get("tool") == proposed_tool and eff.get("args_hash") == expected_args_hash:
                matched_authorized_intent = True

        if not matched_authorized_intent:
            raise VerificationFailedError(
                f"No authorized SUCCEEDED effect matches tool='{proposed_tool}' and args_hash='{expected_args_hash}'."
            )

        return output_payload

    @classmethod
    def compute_expected_evidence(
        cls,
        spec: TaskSpec,
        *,
        effects: list[dict[str, Any]],
        output_payload: dict[str, Any],
        execution_id: str,
        epoch: int,
        revision: int,
    ) -> tuple[str, dict[str, Any]]:
        evidence_obj = {
            "effect_evidence_schema_version": EFFECT_EVIDENCE_SCHEMA_VERSION,
            "unique_key": spec.unique_key,
            "acceptance_hash": spec.acceptance_hash,
            "execution_id": execution_id,
            "epoch": epoch,
            "revision": revision,
            "effects": [cls._canonical_effect_snapshot(e) for e in effects],
            "output_hash": output_payload["output_hash"],
            "output_payload_hash": canonical_json_hash(output_payload),
        }
        return canonical_json_hash(evidence_obj), evidence_obj

    def verify(
        self,
        spec: TaskSpec,
        *,
        effects: list[dict[str, Any]],
        output_payload: dict[str, Any] | None,
        execution_id: str,
        epoch: int,
        revision: int,
    ) -> VerificationReceipt:
        validated_output = self._validate_acceptance_criteria(
            spec,
            effects=effects,
            output_payload=output_payload,
            execution_id=execution_id,
            epoch=epoch,
        )
        evidence_hash, evidence_obj = self.compute_expected_evidence(
            spec,
            effects=effects,
            output_payload=validated_output,
            execution_id=execution_id,
            epoch=epoch,
            revision=revision,
        )
        return VerificationReceipt(
            verification_id=str(uuid.uuid4()),
            task_unique_key=spec.unique_key,
            acceptance_hash=spec.acceptance_hash,
            execution_id=execution_id,
            epoch=epoch,
            revision=revision,
            passed=True,
            evidence_hash=evidence_hash,
            evidence_json=json.dumps(evidence_obj, sort_keys=True, ensure_ascii=False),
            verifier_name=("independent-native-evidence-v1" if "worker_log" in
                           validated_output.get("verified_evidence", {}) else self.name),
            verified_at_utc=self.clock.now_iso(),
        )

    @classmethod
    def replay_verify(
        cls,
        receipt: VerificationReceipt,
        spec: TaskSpec,
        *,
        effects: list[dict[str, Any]],
        output_payload: dict[str, Any] | None,
    ) -> bool:
        """Independently re-run ALL acceptance criteria and recompute full versioned Effect & output hashes."""
        if not receipt.passed:
            return False
        if receipt.task_unique_key != spec.unique_key or receipt.acceptance_hash != spec.acceptance_hash:
            return False
        try:
            validated_output = cls._validate_acceptance_criteria(
                spec,
                effects=effects,
                output_payload=output_payload,
                execution_id=receipt.execution_id,
                epoch=receipt.epoch,
            )
            expected_hash, expected_obj = cls.compute_expected_evidence(
                spec,
                effects=effects,
                output_payload=validated_output,
                execution_id=receipt.execution_id,
                epoch=receipt.epoch,
                revision=receipt.revision,
            )
        except (VerificationFailedError, KeyError, TypeError, ValueError):
            return False

        if receipt.evidence_hash != expected_hash:
            return False
        try:
            return json.loads(receipt.evidence_json) == expected_obj
        except (TypeError, ValueError):
            return False
