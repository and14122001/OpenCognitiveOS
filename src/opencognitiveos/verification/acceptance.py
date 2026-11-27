"""S26: Independent Acceptance Specification Tamper Protection."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from opencognitiveos.verification.seals import seal
from opencognitiveos.contracts.models import (
    Clock,
    SystemClock,
    TaskSpec,
    TaskStatus,
    canonical_json_hash,
)
from opencognitiveos.errors import PolicyDeniedError, VerificationFailedError
if TYPE_CHECKING:
    from opencognitiveos.kernel.authority import ExecutionAuthority

ACCEPTANCE_SPEC_SCHEMA_VERSION = "2.0.0"

_FROZEN_STATUSES = {
    TaskStatus.RUNNING.value,
    TaskStatus.WAITING_APPROVAL.value,
    TaskStatus.WAITING_CHILDREN.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.RETRY_WAIT.value,
    TaskStatus.NEEDS_RECONCILIATION.value,
    TaskStatus.SUCCEEDED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}


def __canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class AcceptanceSpec:
    """Versioned, deterministically serialized acceptance specification bound to TaskSpec & Policy."""

    project_id: str
    repo_id: str
    task_unique_key: str
    base_sha: str
    policy_hash: str
    verifier_id: str
    criteria: dict[str, Any]
    created_at_utc: str
    expires_at_utc: str | None = None
    schema_version: str = ACCEPTANCE_SPEC_SCHEMA_VERSION

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "repo_id": self.repo_id,
            "task_unique_key": self.task_unique_key,
            "base_sha": self.base_sha,
            "policy_hash": self.policy_hash,
            "verifier_id": self.verifier_id,
            "criteria": self.criteria,
            "created_at_utc": self.created_at_utc,
            "expires_at_utc": self.expires_at_utc,
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_canonical_dict())

    def compute_spec_hash(self) -> str:
        return canonical_json_hash(self.to_canonical_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AcceptanceSpec:
        return cls(
            schema_version=str(data["schema_version"]),
            project_id=str(data["project_id"]),
            repo_id=str(data["repo_id"]),
            task_unique_key=str(data["task_unique_key"]),
            base_sha=str(data["base_sha"]),
            policy_hash=str(data["policy_hash"]),
            verifier_id=str(data["verifier_id"]),
            criteria=dict(data["criteria"]),
            created_at_utc=str(data["created_at_utc"]),
            expires_at_utc=str(data["expires_at_utc"]) if data.get("expires_at_utc") is not None else None,
        )


class AcceptanceSpecRegistry:
    """Controller-managed SQLite store for immutable AcceptanceSpecs (S26)."""

    def __init__(self, authority: ExecutionAuthority, clock: Clock | None = None, *, controller_capability: object | None = None, signing_key: bytes | None = None) -> None:
        self._controller_capability = controller_capability if controller_capability is not None else object()
        self._signing_key = signing_key
        self.authority = authority
        self.store = authority.store
        self.clock: Clock = clock or authority.clock
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS acceptance_seals (digest TEXT PRIMARY KEY, tag TEXT NOT NULL)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS acceptance_specs (
                    task_unique_key TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    repo_id TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    verifier_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    spec_hash TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    expires_at_utc TEXT,
                    FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE
                );
                """
            )

    def register_spec(
        self,
        spec: AcceptanceSpec,
        *,
        task_spec: TaskSpec,
        actor_role: str = "controller",
        capability: object | None = None,
    ) -> str:
        """Register an immutable AcceptanceSpec; Workers are strictly forbidden."""
        if capability is not self._controller_capability or actor_role != "controller":
            raise PolicyDeniedError(f"Actor role '{actor_role}' cannot register or modify AcceptanceSpec.")

        if (
            spec.project_id != task_spec.project_id
            or spec.repo_id != task_spec.repo_id
            or spec.task_unique_key != task_spec.unique_key
            or spec.base_sha != task_spec.base_sha
        ):
            raise VerificationFailedError("AcceptanceSpec binding mismatch with TaskSpec.")

        if not spec.verifier_id or not spec.policy_hash:
            raise VerificationFailedError("AcceptanceSpec requires non-empty verifier_id and policy_hash.")

        spec_hash = spec.compute_spec_hash()
        spec_json = spec.to_canonical_json()

        with self.store.transaction() as conn:
            task_row = conn.execute(
                "SELECT status, spec_json, content_hash FROM tasks WHERE unique_key = ?;",
                (task_spec.unique_key,),
            ).fetchone()
            if task_row is None:
                raise VerificationFailedError(f"Task '{task_spec.unique_key}' not found for AcceptanceSpec.")

            persisted = self.authority._spec_from_row(task_row)
            if persisted.content_hash() != task_spec.content_hash() or persisted.content_hash() != task_row["content_hash"]:
                raise VerificationFailedError("Caller TaskSpec differs from persisted task.")

            existing = conn.execute(
                "SELECT spec_hash FROM acceptance_specs WHERE task_unique_key = ?;",
                (task_spec.unique_key,),
            ).fetchone()
            if existing is not None:
                if existing["spec_hash"] == spec_hash:
                    return spec_hash
                if task_row["status"] in _FROZEN_STATUSES:
                    raise PolicyDeniedError(
                        f"Cannot modify AcceptanceSpec after task '{task_spec.unique_key}' has started ({task_row['status']})."
                    )
                raise PolicyDeniedError(
                    f"AcceptanceSpec for '{task_spec.unique_key}' is already registered and immutable."
                )

            if task_row["status"] in _FROZEN_STATUSES:
                raise PolicyDeniedError(
                    f"Cannot register new AcceptanceSpec after task '{task_spec.unique_key}' entered {task_row['status']}."
                )

            conn.execute(
                """
                INSERT INTO acceptance_specs (
                    task_unique_key, project_id, repo_id, base_sha, policy_hash,
                    verifier_id, schema_version, spec_hash, spec_json,
                    created_at_utc, expires_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    spec.task_unique_key,
                    spec.project_id,
                    spec.repo_id,
                    spec.base_sha,
                    spec.policy_hash,
                    spec.verifier_id,
                    spec.schema_version,
                    spec_hash,
                    spec_json,
                    spec.created_at_utc,
                    spec.expires_at_utc,
                ),
            )
            if self._signing_key is not None:
                conn.execute("INSERT INTO acceptance_seals VALUES (?,?)", (spec_hash, seal(self._signing_key, "spec", spec_hash)))
        return spec_hash

    def load_and_verify_spec(
        self,
        *,
        task_spec: TaskSpec,
        expected_policy_hash: str,
        expected_spec_hash: str | None = None,
        verifier_id: str | None = None,
    ) -> tuple[AcceptanceSpec, str]:
        """Re-read trusted AcceptanceSpec from SQLite and verify hash, bindings, and expiry."""
        with self.store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM acceptance_specs WHERE task_unique_key = ?;",
                (task_spec.unique_key,),
            ).fetchone()
        if row is None:
            raise VerificationFailedError(
                f"Missing trusted AcceptanceSpec for task '{task_spec.unique_key}' (Fail Closed)."
            )

        try:
            parsed = json.loads(str(row["spec_json"]))
            spec = AcceptanceSpec.from_dict(parsed)
        except (TypeError, ValueError, KeyError) as exc:
            raise VerificationFailedError(f"Corrupt AcceptanceSpec JSON: {exc}") from exc

        recomputed_hash = spec.compute_spec_hash()
        if recomputed_hash != str(row["spec_hash"]):
            raise VerificationFailedError(
                f"AcceptanceSpec tampered: stored hash {row['spec_hash']} != recomputed {recomputed_hash}."
            )
        if expected_spec_hash is not None and recomputed_hash != expected_spec_hash:
            raise VerificationFailedError(
                f"AcceptanceSpec hash mismatch: expected {expected_spec_hash}, got {recomputed_hash}."
            )
        if (
            spec.project_id != task_spec.project_id
            or spec.repo_id != task_spec.repo_id
            or spec.task_unique_key != task_spec.unique_key
            or spec.base_sha != task_spec.base_sha
        ):
            raise VerificationFailedError("AcceptanceSpec cross-task/cross-project/base_sha binding mismatch.")
        if spec.policy_hash != expected_policy_hash:
            raise VerificationFailedError(
                f"AcceptanceSpec policy_hash mismatch: expected {expected_policy_hash}, got {spec.policy_hash}."
            )
        if verifier_id is not None and spec.verifier_id != verifier_id:
            raise VerificationFailedError(
                f"Verifier identity mismatch: expected '{spec.verifier_id}', got '{verifier_id}'."
            )
        if spec.expires_at_utc is not None:
            if _parse_utc(spec.expires_at_utc) <= self.clock.now_utc():
                raise VerificationFailedError(
                    f"AcceptanceSpec for '{task_spec.unique_key}' expired at {spec.expires_at_utc}."
                )

        return spec, recomputed_hash

    @staticmethod
    def assert_verifier_authority(*, actor_role: str, verifier_id: str, trusted_spec: AcceptanceSpec) -> None:
        """Prevent Worker from submitting PASS or impersonating independent verifier."""
        if actor_role not in {"verifier", "controller"}:
            raise PolicyDeniedError(
                f"Actor role '{actor_role}' is forbidden from submitting verification PASS."
            )
        if verifier_id != trusted_spec.verifier_id:
            raise VerificationFailedError(
                f"Forged verifier identity '{verifier_id}'; expected '{trusted_spec.verifier_id}'."
            )
