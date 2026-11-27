"""Versioned data contracts and protocols for OpenCognitiveOS M0-M1.2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

CONTRACT_SCHEMA_VERSION = "1.6.0"


def canonical_json_hash(payload: Any) -> str:
    """Return deterministic SHA-256 hex digest for JSON-serializable data."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TaskStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    PLANNED = "PLANNED"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_CHILDREN = "WAITING_CHILDREN"
    VERIFYING = "VERIFYING"
    RETRY_WAIT = "RETRY_WAIT"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.SUBMITTED: frozenset({TaskStatus.PLANNED, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.PLANNED: frozenset({TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.READY: frozenset(
        {TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_CHILDREN,
            TaskStatus.RETRY_WAIT,
            TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.WAITING_CHILDREN: frozenset(
        {TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.RETRY_WAIT,
            TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RETRY_WAIT: frozenset({TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.NEEDS_RECONCILIATION: frozenset(
        {TaskStatus.VERIFYING, TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

WORKER_FENCED_TRANSITIONS: frozenset[tuple[TaskStatus, TaskStatus]] = frozenset(
    {
        (TaskStatus.READY, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.VERIFYING),
        (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL),
        (TaskStatus.RUNNING, TaskStatus.WAITING_CHILDREN),
        (TaskStatus.RUNNING, TaskStatus.NEEDS_RECONCILIATION),
        (TaskStatus.VERIFYING, TaskStatus.SUCCEEDED),
        (TaskStatus.VERIFYING, TaskStatus.NEEDS_RECONCILIATION),
    }
)

# M1.5 P0-01: Explicit Public Operation x Allowed TaskStatus Matrix
OPERATION_ALLOWED_STATES: dict[str, frozenset[TaskStatus]] = {
    "acquire_lease": frozenset({TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.VERIFYING}),
    "heartbeat_lease": frozenset({TaskStatus.RUNNING, TaskStatus.VERIFYING}),
    "record_tool_intent": frozenset({TaskStatus.RUNNING}),
    "record_effect_receipt": frozenset({TaskStatus.RUNNING}),
    "verify_and_complete": frozenset({TaskStatus.VERIFYING}),
    "reconcile_effect": frozenset({TaskStatus.NEEDS_RECONCILIATION}),
    "schedule_retry": frozenset({TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.RETRY_WAIT}),
    "cancel_task": frozenset(
        {
            TaskStatus.SUBMITTED,
            TaskStatus.PLANNED,
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_CHILDREN,
            TaskStatus.VERIFYING,
            TaskStatus.RETRY_WAIT,
            TaskStatus.NEEDS_RECONCILIATION,
        }
    ),
}


class DataClass(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class EffectOutcome(str, Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class IntentDisposition(str, Enum):
    CREATED = "CREATED"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    SETTLED = "SETTLED"


@dataclass(frozen=True)
class Budget:
    max_retries: int = 3
    max_cost_usd: float = 0.0
    max_steps: int = 10
    lease_seconds: int = 120
    heartbeat_seconds: int = 30

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Budget:
        return cls(
            max_retries=int(data.get("max_retries", 3)),
            max_cost_usd=float(data.get("max_cost_usd", 0.0)),
            max_steps=int(data.get("max_steps", 10)),
            lease_seconds=int(data.get("lease_seconds", 120)),
            heartbeat_seconds=int(data.get("heartbeat_seconds", 30)),
        )


@dataclass(frozen=True)
class TaskSpec:
    project_id: str
    repo_id: str
    task_id: str
    goal: str
    acceptance_hash: str
    schema_version: str = CONTRACT_SCHEMA_VERSION
    parent_id: str | None = None
    base_sha: str = "HEAD"
    allowed_paths: tuple[str, ...] = ()
    data_class: DataClass = DataClass.INTERNAL
    allowed_providers: tuple[str, ...] = ("mock-local",)
    allowed_tools: tuple[str, ...] = ("mock.echo",)
    cloud_approved: bool = False
    deadline: str | None = None
    budget: Budget = field(default_factory=Budget)
    dependencies: tuple[str, ...] = ()

    @property
    def unique_key(self) -> str:
        return f"{self.repo_id}::{self.task_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "repo_id": self.repo_id,
            "task_id": self.task_id,
            "parent_id": self.parent_id,
            "base_sha": self.base_sha,
            "goal": self.goal,
            "acceptance_hash": self.acceptance_hash,
            "allowed_paths": list(self.allowed_paths),
            "data_class": self.data_class.value,
            "allowed_providers": list(self.allowed_providers),
            "allowed_tools": list(self.allowed_tools),
            "cloud_approved": self.cloud_approved,
            "deadline": self.deadline,
            "budget": self.budget.to_dict(),
            "dependencies": list(self.dependencies),
        }

    def content_hash(self) -> str:
        return canonical_json_hash(self.to_dict())


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    task_id: str
    execution_id: str
    epoch: int
    sequence: int
    causation_id: str | None
    correlation_id: str
    occurred_at_utc: str
    payload_ref: str
    payload_hash: str
    schema_version: str = CONTRACT_SCHEMA_VERSION


@dataclass(frozen=True)
class ToolIntent:
    intent_id: str
    execution_id: str
    epoch: int
    tool: str
    args_hash: str
    approval_ref: str
    idempotency_key: str


@dataclass(frozen=True)
class ToolIntentSubmission:
    intent: ToolIntent
    outcome: EffectOutcome
    disposition: IntentDisposition

    @property
    def should_execute(self) -> bool:
        return self.disposition == IntentDisposition.CREATED


@dataclass(frozen=True)
class EffectReceipt:
    intent_id: str
    outcome: EffectOutcome
    external_ref: str | None
    evidence_hash: str


@dataclass(frozen=True)
class VerificationReceipt:
    verification_id: str
    task_unique_key: str
    acceptance_hash: str
    execution_id: str
    epoch: int
    revision: int
    passed: bool
    evidence_hash: str
    evidence_json: str
    verifier_name: str
    verified_at_utc: str


class Clock(Protocol):
    def now_utc(self) -> datetime: ...

    def now_iso(self) -> str: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return self.now_utc().isoformat().replace("+00:00", "Z")


class InjectableClock:
    """Deterministic UTC clock for tests without sleep."""

    def __init__(self, initial_utc: datetime | None = None) -> None:
        if initial_utc is None:
            initial_utc = datetime(2026, 11, 27, 0, 0, 0, tzinfo=timezone.utc)
        if initial_utc.tzinfo is None:
            initial_utc = initial_utc.replace(tzinfo=timezone.utc)
        self._current = initial_utc.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self._current

    def now_iso(self) -> str:
        return self._current.isoformat().replace("+00:00", "Z")

    def advance(self, seconds: float) -> datetime:
        self._current = self._current + timedelta(seconds=seconds)
        return self._current


@runtime_checkable
class Provider(Protocol):
    name: str
    is_cloud: bool

    def capabilities(self) -> dict[str, Any]: ...

    def infer(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def cancel(self, run_id: str) -> bool: ...

    def quota(self) -> dict[str, Any]: ...


@runtime_checkable
class Worker(Protocol):
    def start(self, spec: TaskSpec) -> str: ...

    def events(self, run_id: str) -> list[Event]: ...

    def cancel(self, run_id: str) -> bool: ...

    def reconcile(self, run_id: str) -> EffectReceipt | None: ...


@runtime_checkable
class World(Protocol):
    def observe(self) -> dict[str, Any]: ...

    def snapshot(self) -> dict[str, Any]: ...

    def predict(self, snapshot: dict[str, Any], action: dict[str, Any], horizon: int) -> dict[str, Any]: ...

    def compare(self, observation: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]: ...
