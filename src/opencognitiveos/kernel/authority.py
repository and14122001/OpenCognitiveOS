"""Single deterministic execution authority backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from opencognitiveos.contracts.models import (
    ALLOWED_TRANSITIONS,
    CONTRACT_SCHEMA_VERSION,
    OPERATION_ALLOWED_STATES,
    TERMINAL_STATES,
    WORKER_FENCED_TRANSITIONS,
    Budget,
    Clock,
    DataClass,
    EffectOutcome,
    EffectReceipt,
    IntentDisposition,
    SystemClock,
    TaskSpec,
    TaskStatus,
    ToolIntent,
    ToolIntentSubmission,
    VerificationReceipt,
    canonical_json_hash,
)
from opencognitiveos.errors import (
    BudgetExceededError,
    CASConflictError,
    DatabaseIntegrityError,
    InvalidTransitionError,
    LeaseConflictError,
    LeaseExpiredError,
    PolicyDeniedError,
    ReconciliationRequiredError,
    StaleEpochError,
    TaskConflictError,
    TaskNotFoundError,
    VerificationFailedError,
)
from opencognitiveos.kernel.policy import PolicyGate
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.verifier import IndependentVerifier


def _parse_utc(iso_str: str) -> datetime:
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TaskRecord:
    unique_key: str
    project_id: str
    repo_id: str
    task_id: str
    content_hash: str
    status: TaskStatus
    revision: int
    fencing_epoch: int
    retry_count: int
    step_count: int
    spec: TaskSpec
    result: dict[str, Any] | None
    error_message: str | None
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True)
class LeaseRecord:
    task_unique_key: str
    execution_id: str
    worker_id: str
    fencing_epoch: int
    acquired_at_utc: str
    heartbeat_at_utc: str
    expires_at_utc: str


class ExecutionAuthority:
    """Single writer for task lifecycle, leases, outbox events, effect intents, and verification receipts."""

    def __init__(
        self,
        store: SQLiteStore,
        clock: Clock | None = None,
        policy_gate: PolicyGate | None = None,
        verifier: IndependentVerifier | None = None,
    ) -> None:
        self._controller_capability = object()
        self._broker_capability = object()
        self.evidence_completion = None
        self.store = store
        self.clock: Clock = clock or store.clock or SystemClock()
        self.policy_gate = policy_gate or PolicyGate()
        self.verifier = verifier or IndependentVerifier(clock=self.clock)

    @staticmethod
    def _spec_from_row(row: sqlite3.Row) -> TaskSpec:
        raw = json.loads(row["spec_json"])
        return TaskSpec(
            schema_version=raw["schema_version"],
            project_id=raw["project_id"],
            repo_id=raw["repo_id"],
            task_id=raw["task_id"],
            parent_id=raw.get("parent_id"),
            base_sha=raw.get("base_sha", "HEAD"),
            goal=raw["goal"],
            acceptance_hash=raw["acceptance_hash"],
            allowed_paths=tuple(raw.get("allowed_paths", ())),
            data_class=DataClass(raw.get("data_class", DataClass.INTERNAL.value)),
            allowed_providers=tuple(raw.get("allowed_providers", ("mock-local",))),
            allowed_tools=tuple(raw.get("allowed_tools", ("mock.echo",))),
            cloud_approved=bool(raw.get("cloud_approved", False)),
            deadline=raw.get("deadline"),
            budget=Budget.from_dict(raw.get("budget", {})),
            dependencies=tuple(raw.get("dependencies", ())),
        )

    @classmethod
    def _task_from_row(cls, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            unique_key=row["unique_key"],
            project_id=row["project_id"],
            repo_id=row["repo_id"],
            task_id=row["task_id"],
            content_hash=row["content_hash"],
            status=TaskStatus(row["status"]),
            revision=int(row["revision"]),
            fencing_epoch=int(row["fencing_epoch"]),
            retry_count=int(row["retry_count"]),
            step_count=int(row["step_count"]) if "step_count" in row.keys() else 0,
            spec=cls._spec_from_row(row),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error_message=row["error_message"],
            created_at_utc=row["created_at_utc"],
            updated_at_utc=row["updated_at_utc"],
        )

    @staticmethod
    def _check_deadline(spec: TaskSpec, now_dt: datetime) -> None:
        if spec.deadline:
            if _parse_utc(spec.deadline) <= now_dt:
                raise BudgetExceededError(
                    f"Task '{spec.unique_key}' exceeded deadline {spec.deadline} (now={_format_utc(now_dt)})."
                )

    @staticmethod
    def _assert_operation_allowed(operation: str, status: TaskStatus, unique_key: str) -> None:
        allowed = OPERATION_ALLOWED_STATES[operation]
        if status not in allowed:
            raise InvalidTransitionError(
                f"Operation '{operation}' is not allowed in status {status.value} on '{unique_key}'."
            )

    def _next_sequence(self, conn: sqlite3.Connection, unique_key: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS seq FROM events WHERE task_unique_key = ?;",
            (unique_key,),
        ).fetchone()
        return int(row["seq"]) + 1

    def _append_event_and_outbox(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        task_unique_key: str,
        execution_id: str,
        epoch: int,
        payload: dict[str, Any],
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        seq = self._next_sequence(conn, task_unique_key)
        now_iso = self.clock.now_iso()
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        payload_hash = canonical_json_hash(payload)
        payload_ref = f"inline://{task_unique_key}/{seq}"
        corr = correlation_id or task_unique_key

        conn.execute(
            """
            INSERT INTO events (
                event_id, type, schema_version, task_unique_key, execution_id,
                epoch, sequence, causation_id, correlation_id, occurred_at_utc,
                payload_ref, payload_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                event_id,
                event_type,
                CONTRACT_SCHEMA_VERSION,
                task_unique_key,
                execution_id,
                epoch,
                seq,
                causation_id,
                corr,
                now_iso,
                payload_ref,
                payload_hash,
                payload_json,
            ),
        )
        conn.execute(
            """
            INSERT INTO outbox (event_id, task_unique_key, topic, payload_json, status, created_at_utc)
            VALUES (?, ?, ?, ?, 'PENDING', ?);
            """,
            (event_id, task_unique_key, event_type, payload_json, now_iso),
        )
        return event_id

    @staticmethod
    def _validate_active_lease(
        conn: sqlite3.Connection,
        *,
        unique_key: str,
        current_epoch: int,
        worker_id: str | None,
        execution_id: str | None,
        epoch: int | None,
        now_dt: datetime,
    ) -> sqlite3.Row:
        task = conn.execute("SELECT project_id, repo_id, parent_id FROM tasks WHERE unique_key=?", (unique_key,)).fetchone()
        if task is None:
            raise TaskNotFoundError(unique_key)
        ExecutionAuthority._check_ancestors(conn, task)
        if epoch is None or not worker_id or not execution_id:
            raise StaleEpochError(
                f"Worker operation on '{unique_key}' requires worker_id, execution_id, and epoch."
            )
        if epoch != current_epoch:
            raise StaleEpochError(
                f"Stale fencing epoch for '{unique_key}': got {epoch}, current {current_epoch}."
            )
        lease_row = conn.execute(
            "SELECT * FROM leases WHERE task_unique_key = ?;", (unique_key,)
        ).fetchone()
        if (
            lease_row is None
            or int(lease_row["fencing_epoch"]) != epoch
            or lease_row["worker_id"] != worker_id
            or lease_row["execution_id"] != execution_id
        ):
            raise StaleEpochError(
                f"Lease ownership mismatch for '{unique_key}' (worker={worker_id}, execution={execution_id}, epoch={epoch})."
            )
        if _parse_utc(lease_row["expires_at_utc"]) <= now_dt:
            raise LeaseExpiredError(
                f"Lease for '{unique_key}' (epoch {epoch}) expired at {lease_row['expires_at_utc']}."
            )
        return lease_row

    def get_task(self, unique_key: str) -> TaskRecord:
        with self.store.open_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")
            return self._task_from_row(row)

    def get_effects(self, unique_key: str, *, execution_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.open_connection() as conn:
            if execution_id is None:
                rows = conn.execute(
                    "SELECT * FROM effects WHERE task_unique_key = ? ORDER BY created_at_utc ASC;",
                    (unique_key,),
                ).fetchall()
                return [dict(r) for r in rows]
            rows = conn.execute(
                """
                SELECT e.*, er.execution_id AS reused_for_execution_id
                FROM effects e
                LEFT JOIN effect_reuses er
                    ON er.intent_id = e.intent_id AND er.execution_id = ?
                WHERE e.task_unique_key = ?
                  AND (e.execution_id = ? OR er.execution_id = ?)
                ORDER BY e.created_at_utc ASC;
                """,
                (execution_id, unique_key, execution_id, execution_id),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_verification_receipts(self, unique_key: str) -> list[VerificationReceipt]:
        with self.store.open_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM verification_receipts WHERE task_unique_key = ? ORDER BY verified_at_utc ASC;",
                (unique_key,),
            ).fetchall()
            return [
                VerificationReceipt(
                    verification_id=r["verification_id"],
                    task_unique_key=r["task_unique_key"],
                    acceptance_hash=r["acceptance_hash"],
                    execution_id=r["execution_id"],
                    epoch=int(r["epoch"]),
                    revision=int(r["revision"]),
                    passed=bool(r["passed"]),
                    evidence_hash=r["evidence_hash"],
                    evidence_json=r["evidence_json"],
                    verifier_name=r["verifier_name"],
                    verified_at_utc=r["verified_at_utc"],
                )
                for r in rows
            ]

    def replay_verification(self, unique_key: str) -> bool:
        """Independently replay persisted verification evidence from SQLite without Worker input."""
        with self.store.open_connection() as conn:
            t_row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if t_row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")
            spec = self._spec_from_row(t_row)
            v_row = conn.execute(
                "SELECT * FROM verification_receipts WHERE task_unique_key = ? ORDER BY verified_at_utc DESC LIMIT 1;",
                (unique_key,),
            ).fetchone()
            if v_row is None:
                return False
            receipt = VerificationReceipt(
                verification_id=v_row["verification_id"],
                task_unique_key=v_row["task_unique_key"],
                acceptance_hash=v_row["acceptance_hash"],
                execution_id=v_row["execution_id"],
                epoch=int(v_row["epoch"]),
                revision=int(v_row["revision"]),
                passed=bool(v_row["passed"]),
                evidence_hash=v_row["evidence_hash"],
                evidence_json=v_row["evidence_json"],
                verifier_name=v_row["verifier_name"],
                verified_at_utc=v_row["verified_at_utc"],
            )
            output_payload = json.loads(v_row["output_payload_json"])
            effects = json.loads(v_row["effects_snapshot_json"])
            if not self._replay_sealed(conn, spec, receipt, output_payload):
                return False
        return IndependentVerifier.replay_verify(receipt, spec, effects=effects, output_payload=output_payload)

    def _replay_sealed(self, conn, spec, receipt, output_payload):
        binding = output_payload.get("verified_evidence") if isinstance(output_payload, dict) else None
        if binding is None:
            return spec.base_sha == "HEAD"
        if self.evidence_completion is None or not isinstance(binding, dict):
            return False
        try:
            return self.evidence_completion.replay(conn, spec, binding, receipt.execution_id, receipt.epoch)
        except (VerificationFailedError, KeyError, ValueError, TypeError, OSError, sqlite3.Error):
            return False

    def audit_database_consistency(self, unique_key: str) -> dict[str, Any]:
        """Single-transaction cross-table consistency audit (NOT defense against full-DB-write attackers)."""
        with self.store.open_connection() as conn:
            conn.execute("BEGIN DEFERRED;")
            try:
                integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check;").fetchall()]
                fk_rows = [dict(row) for row in conn.execute("PRAGMA foreign_key_check;").fetchall()]
                fk_enabled = int(conn.execute("PRAGMA foreign_keys;").fetchone()[0])
                if integrity_rows != ["ok"] or len(fk_rows) != 0 or fk_enabled != 1:
                    raise DatabaseIntegrityError(
                        f"SQLite integrity/FK check failed: integrity={integrity_rows}, fk={fk_rows}."
                    )

                t_row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
                if t_row is None:
                    raise TaskNotFoundError(f"Task '{unique_key}' not found.")
                if TaskStatus(t_row["status"]) != TaskStatus.SUCCEEDED:
                    raise DatabaseIntegrityError(
                        f"Task '{unique_key}' has status '{t_row['status']}', expected SUCCEEDED."
                    )

                v_row = conn.execute(
                    "SELECT * FROM verification_receipts WHERE task_unique_key = ? ORDER BY verified_at_utc DESC LIMIT 1;",
                    (unique_key,),
                ).fetchone()
                if v_row is None:
                    raise DatabaseIntegrityError(f"Missing verification_receipts row for '{unique_key}'.")

                spec = self._spec_from_row(t_row)
                receipt = VerificationReceipt(
                    verification_id=v_row["verification_id"],
                    task_unique_key=v_row["task_unique_key"],
                    acceptance_hash=v_row["acceptance_hash"],
                    execution_id=v_row["execution_id"],
                    epoch=int(v_row["epoch"]),
                    revision=int(v_row["revision"]),
                    passed=bool(v_row["passed"]),
                    evidence_hash=v_row["evidence_hash"],
                    evidence_json=v_row["evidence_json"],
                    verifier_name=v_row["verifier_name"],
                    verified_at_utc=v_row["verified_at_utc"],
                )
                try:
                    output_payload = json.loads(v_row["output_payload_json"])
                    snapshot_effects = json.loads(v_row["effects_snapshot_json"])
                except (TypeError, ValueError) as exc:
                    raise DatabaseIntegrityError(f"Corrupt verification_receipts JSON for '{unique_key}': {exc}") from exc

                if not self._replay_sealed(conn, spec, receipt, output_payload) or not IndependentVerifier.replay_verify(
                    receipt, spec, effects=snapshot_effects, output_payload=output_payload
                ):
                    raise DatabaseIntegrityError(f"Historical snapshot replay failed for '{unique_key}'.")

                # Exact full-object comparison of tasks.result_json (rejects add/delete/modify/type-error)
                try:
                    result_obj = json.loads(t_row["result_json"]) if t_row["result_json"] is not None else None
                except (TypeError, ValueError) as exc:
                    raise DatabaseIntegrityError(f"Corrupt tasks.result_json for '{unique_key}': {exc}") from exc

                expected_result = {
                    "output_hash": output_payload["output_hash"],
                    "verification_id": v_row["verification_id"],
                    "verification_evidence_hash": v_row["evidence_hash"],
                }
                if type(result_obj) is not dict or set(result_obj.keys()) != set(expected_result.keys()):
                    raise DatabaseIntegrityError(
                        f"Task '{unique_key}' result_json keys/type mismatch: got {result_obj!r}."
                    )
                for k, expected_val in expected_result.items():
                    actual_val = result_obj.get(k)
                    if type(actual_val) is not str or actual_val != expected_val:
                        raise DatabaseIntegrityError(
                            f"Task '{unique_key}' result_json.{k} mismatch: got {actual_val!r}, expected {expected_val!r}."
                        )

                for snap in snapshot_effects:
                    live = conn.execute(
                        "SELECT * FROM effects WHERE intent_id = ?;", (snap["intent_id"],)
                    ).fetchone()
                    if live is None:
                        raise DatabaseIntegrityError(
                            f"Live effects row '{snap['intent_id']}' missing for '{unique_key}'."
                        )
                    for field in (
                        "task_unique_key",
                        "execution_id",
                        "tool",
                        "args_hash",
                        "approval_ref",
                        "idempotency_key",
                        "outcome",
                        "external_ref",
                        "evidence_hash",
                    ):
                        if live[field] != snap.get(field):
                            raise DatabaseIntegrityError(
                                f"Live DB tampering detected on effects.{field} for '{snap['intent_id']}': "
                                f"live='{live[field]}' != snapshot='{snap.get(field)}'."
                            )
                    if int(live["epoch"]) != int(snap["epoch"]):
                        raise DatabaseIntegrityError(
                            f"Live DB tampering detected on effects.epoch for '{snap['intent_id']}'."
                        )

                result_summary = {
                    "ok": True,
                    "unique_key": unique_key,
                    "verification_id": v_row["verification_id"],
                    "checked_effects": len(snapshot_effects),
                }
            finally:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
        return result_summary

    @staticmethod
    def _check_ancestors(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        for _ in range(4):
            if row["parent_id"] is None:
                return
            row = conn.execute(
                "SELECT project_id, repo_id, status, parent_id FROM tasks WHERE project_id=? AND repo_id=? AND task_id=?",
                (row["project_id"], row["repo_id"], row["parent_id"]),
            ).fetchone()
            if row is None or row["status"] in ("CANCELLED", "FAILED", "SUCCEEDED"):
                raise PolicyDeniedError("Parent no longer authorizes child execution.")
        raise PolicyDeniedError("Invalid persisted task ancestry.")

    @staticmethod
    def _reserved_child_steps(conn: sqlite3.Connection, spec: TaskSpec) -> int:
        children = conn.execute(
            "SELECT spec_json FROM tasks WHERE repo_id=? AND project_id=? AND parent_id=?",
            (spec.repo_id, spec.project_id, spec.task_id),
        )
        return sum(int(json.loads(row[0])["budget"]["max_steps"]) for row in children)

    def _validate_task_graph(self, conn: sqlite3.Connection, spec: TaskSpec) -> None:
        # Dependencies only point to already admitted tasks. Edges are immutable.
        for key in spec.dependencies:
            if not conn.execute(
                "SELECT 1 FROM tasks WHERE unique_key=? AND project_id=? AND repo_id=?",
                (key, spec.project_id, spec.repo_id),
            ).fetchone():
                raise PolicyDeniedError("Dependency outside task scope or missing.")
        if spec.parent_id is None:
            return
        ancestor_id = spec.parent_id
        for depth in range(1, 5):
            row = conn.execute(
                "SELECT * FROM tasks WHERE repo_id=? AND project_id=? AND task_id=?",
                (spec.repo_id, spec.project_id, ancestor_id),
            ).fetchone()
            if row is None or depth > 3 or row["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                raise PolicyDeniedError("Task ancestry unavailable, terminal, or exceeds depth three.")
            if depth == 1:
                parent = self._spec_from_row(row)
                siblings = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE repo_id=? AND project_id=? AND parent_id=?",
                    (spec.repo_id, spec.project_id, parent.task_id),
                ).fetchone()[0]
                # Child retries and cloud use await a shared reservation ledger.
                if (siblings >= 3 or spec.cloud_approved or spec.budget.max_cost_usd != 0
                        or spec.budget.max_retries != 0 or spec.base_sha != parent.base_sha
                        or spec.deadline != parent.deadline or spec.data_class != parent.data_class
                        or not set(spec.allowed_paths) <= set(parent.allowed_paths)
                        or not set(spec.allowed_providers) <= set(parent.allowed_providers)
                        or not set(spec.allowed_tools) <= set(parent.allowed_tools)):
                    raise PolicyDeniedError("Child exceeds parent scope or branching limits.")
                available = parent.budget.max_steps - row["step_count"] - self._reserved_child_steps(conn, parent)
                if not 0 < spec.budget.max_steps <= available:
                    raise BudgetExceededError("Child step reservation exceeds parent balance.")
            ancestor_id = row["parent_id"]
            if ancestor_id is None:
                return

    def submit_task(self, spec: TaskSpec) -> tuple[TaskRecord, bool]:
        """Submit task idempotently by repo_id::task_id. Returns (record, created)."""
        unique_key = spec.unique_key
        content_hash = spec.content_hash()
        now_iso = self.clock.now_iso()
        spec_json = json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=False)

        with self.store.transaction() as conn:
            existing = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise TaskConflictError(
                        f"Task '{unique_key}' already exists with different content_hash "
                        f"({existing['content_hash']} != {content_hash})."
                    )
                return self._task_from_row(existing), False

            self._validate_task_graph(conn, spec)
            conn.execute(
                """
                INSERT OR IGNORE INTO projects (project_id, name, created_at_utc)
                VALUES (?, ?, ?);
                """,
                (spec.project_id, spec.project_id, now_iso),
            )
            conn.execute(
                """
                INSERT INTO tasks (
                    unique_key, project_id, repo_id, task_id, parent_id, content_hash,
                    schema_version, status, revision, fencing_epoch, retry_count, step_count,
                    spec_json, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 0, ?, ?, ?);
                """,
                (
                    unique_key,
                    spec.project_id,
                    spec.repo_id,
                    spec.task_id,
                    spec.parent_id,
                    content_hash,
                    spec.schema_version,
                    TaskStatus.SUBMITTED.value,
                    spec_json,
                    now_iso,
                    now_iso,
                ),
            )
            for dep in spec.dependencies:
                conn.execute(
                    "INSERT INTO dependencies (task_unique_key, depends_on_unique_key) VALUES (?, ?);",
                    (unique_key, dep),
                )

            self._append_event_and_outbox(
                conn,
                event_type="TASK_SUBMITTED",
                task_unique_key=unique_key,
                execution_id="kernel",
                epoch=0,
                payload={"unique_key": unique_key, "content_hash": content_hash, "status": TaskStatus.SUBMITTED.value},
            )
            row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            return self._task_from_row(row), True

    def _cancel_descendants(self, conn, spec, now_iso):
        rows = conn.execute("""WITH RECURSIVE descendants(task_id) AS (
            SELECT task_id FROM tasks WHERE project_id=? AND repo_id=? AND parent_id=?
            UNION SELECT t.task_id FROM tasks t JOIN descendants d ON t.parent_id=d.task_id
            WHERE t.project_id=? AND t.repo_id=?)
            SELECT t.* FROM tasks t JOIN descendants d ON d.task_id=t.task_id
            WHERE t.project_id=? AND t.repo_id=? AND t.status NOT IN ('SUCCEEDED','FAILED','CANCELLED')""",
            (spec.project_id, spec.repo_id, spec.task_id, spec.project_id, spec.repo_id, spec.project_id, spec.repo_id))
        for child in rows.fetchall():
            key = child["unique_key"]
            pending = conn.execute("SELECT 1 FROM effects WHERE task_unique_key=? AND outcome IN ('PENDING','UNKNOWN')",
                                   (key,)).fetchone()
            state = "NEEDS_RECONCILIATION" if pending else "CANCELLED"
            conn.execute("DELETE FROM leases WHERE task_unique_key=?", (key,))
            conn.execute("UPDATE tasks SET status=?, revision=revision+1, updated_at_utc=? WHERE unique_key=?",
                         (state, now_iso, key))
            self._append_event_and_outbox(conn, event_type="TASK_" + state, task_unique_key=key,
                execution_id="controller", epoch=child["fencing_epoch"],
                payload={"from":child["status"], "to":state, "ancestor":spec.unique_key,
                         "revision":child["revision"]+1})

    def transition_task(
        self,
        unique_key: str,
        target_status: TaskStatus,
        *,
        expected_revision: int,
        actor_role: str = "worker",
        controller_capability: object | None = None,
        epoch: int | None = None,
        worker_id: str | None = None,
        execution_id: str | None = None,
        verification_id: str | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> TaskRecord:
        """Transition task status atomically with CAS revision, role/fencing enforcement, and outbox."""
        if actor_role == "controller" and controller_capability is not self._controller_capability:
            raise PolicyDeniedError("Controller transition requires control-plane authorization.")
        if actor_role not in {"worker", "controller"}:
            raise PolicyDeniedError(f"Invalid actor_role '{actor_role}'.")

        now_dt = self.clock.now_utc()
        now_iso = _format_utc(now_dt)

        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")

            current_status = TaskStatus(row["status"])
            current_rev = int(row["revision"])
            current_epoch = int(row["fencing_epoch"])
            spec = self._spec_from_row(row)

            if current_status in TERMINAL_STATES:
                raise InvalidTransitionError(
                    f"Cannot transition terminal task '{unique_key}' from {current_status.value} to {target_status.value}."
                )
            if target_status not in ALLOWED_TRANSITIONS[current_status]:
                raise InvalidTransitionError(
                    f"Illegal transition for '{unique_key}': {current_status.value} -> {target_status.value}."
                )
            if current_rev != expected_revision:
                raise CASConflictError(
                    f"CAS revision mismatch for '{unique_key}': expected {expected_revision}, actual {current_rev}."
                )

            requires_fencing = (current_status, target_status) in WORKER_FENCED_TRANSITIONS
            if actor_role == "controller" and requires_fencing:
                raise PolicyDeniedError(
                    f"Controller cannot perform worker-fenced transition {current_status.value} -> {target_status.value}."
                )
            if actor_role == "worker" or requires_fencing or any(v is not None for v in (epoch, worker_id, execution_id)):
                self._check_deadline(spec, now_dt)
                self._validate_active_lease(
                    conn,
                    unique_key=unique_key,
                    current_epoch=current_epoch,
                    worker_id=worker_id,
                    execution_id=execution_id,
                    epoch=epoch,
                    now_dt=now_dt,
                )

            if target_status == TaskStatus.SUCCEEDED:
                raise VerificationFailedError("Only the atomic verifier may commit SUCCEEDED.")

            new_rev = current_rev + 1
            result_json = json.dumps(result, sort_keys=True, ensure_ascii=False) if result is not None else row["result_json"]
            err_msg = error_message if error_message is not None else row["error_message"]

            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, revision = ?, result_json = ?, error_message = ?, updated_at_utc = ?
                WHERE unique_key = ? AND revision = ?;
                """,
                (target_status.value, new_rev, result_json, err_msg, now_iso, unique_key, expected_revision),
            )
            if cursor.rowcount != 1:
                raise CASConflictError(f"Concurrent CAS update detected for '{unique_key}'.")

            if target_status in TERMINAL_STATES or target_status in {
                TaskStatus.RETRY_WAIT,
                TaskStatus.NEEDS_RECONCILIATION,
            }:
                conn.execute("DELETE FROM leases WHERE task_unique_key = ?;", (unique_key,))

            if target_status == TaskStatus.CANCELLED:
                self._cancel_descendants(conn, spec, now_iso)

            self._append_event_and_outbox(
                conn,
                event_type=f"TASK_{target_status.value}",
                task_unique_key=unique_key,
                execution_id=execution_id or "controller",
                epoch=current_epoch,
                payload={
                    "unique_key": unique_key,
                    "from": current_status.value,
                    "to": target_status.value,
                    "revision": new_rev,
                    "epoch": current_epoch,
                    "actor_role": actor_role,
                },
            )
            if before_commit_hook is not None:
                before_commit_hook(conn)

            updated = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            return self._task_from_row(updated)

    def verify_and_complete(
        self,
        unique_key: str,
        *,
        worker_id: str,
        execution_id: str,
        epoch: int,
        expected_revision: int,
        evidence_id: str | None = None,
        verifier: IndependentVerifier | None = None,
        output_payload: dict[str, Any] | None = None,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> tuple[TaskRecord, VerificationReceipt]:
        """Atomically run controller-managed verifier, re-validate live lease & deadline before commit, and complete."""
        if verifier is not None and verifier is not self.verifier:
            raise PolicyDeniedError("Worker cannot substitute controller-managed verifier.")

        now_dt = self.clock.now_utc()

        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")

            current_status = TaskStatus(row["status"])
            current_rev = int(row["revision"])
            current_epoch = int(row["fencing_epoch"])
            spec = self._spec_from_row(row)

            # R4 tasks must not fall back to the legacy M1 verifier.
            # Only the explicitly offline mock contract may use the M1 verifier.
            registry_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='acceptance_specs'"
            ).fetchone()
            workspace_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspaces'"
            ).fetchone()
            has_workspace = workspace_table and conn.execute(
                "SELECT 1 FROM workspaces WHERE task_unique_key=?", (unique_key,)
            ).fetchone()
            registered = registry_exists and conn.execute(
                "SELECT 1 FROM acceptance_specs WHERE task_unique_key=?", (unique_key,)
            ).fetchone()
            mock_only = (spec.base_sha == "HEAD" and not spec.cloud_approved
                         and spec.allowed_providers == ("mock-local",)
                         and set(spec.allowed_tools) <= {"mock.echo", "mock.write_note"})
            governed = (not mock_only or self.evidence_completion is not None or has_workspace
                        or registered or evidence_id is not None)
            if governed and (not has_workspace or not registered or not evidence_id or self.evidence_completion is None):
                raise VerificationFailedError("Governed completion requires a trusted manifest and workspace; legacy completion denied.")

            if current_status != TaskStatus.VERIFYING:
                raise InvalidTransitionError(
                    f"verify_and_complete requires status VERIFYING, got {current_status.value}."
                )
            if current_rev != expected_revision:
                raise CASConflictError(
                    f"CAS revision mismatch for '{unique_key}': expected {expected_revision}, actual {current_rev}."
                )

            self._check_deadline(spec, now_dt)
            self._validate_active_lease(
                conn,
                unique_key=unique_key,
                current_epoch=current_epoch,
                worker_id=worker_id,
                execution_id=execution_id,
                epoch=epoch,
                now_dt=now_dt,
            )

            eff_rows = conn.execute(
                """
                SELECT e.*, er.execution_id AS reused_for_execution_id
                FROM effects e
                LEFT JOIN effect_reuses er
                    ON er.intent_id = e.intent_id AND er.execution_id = ?
                WHERE e.task_unique_key = ?
                  AND ((e.execution_id = ? AND e.epoch = ?) OR er.execution_id = ?)
                ORDER BY e.created_at_utc ASC;
                """,
                (execution_id, unique_key, execution_id, current_epoch, execution_id),
            ).fetchall()
            effects = [dict(r) for r in eff_rows]

            if isinstance(output_payload, dict) and "verified_evidence" in output_payload:
                raise VerificationFailedError("Evidence bindings are reserved for the controller.")
            if governed:
                if self.evidence_completion.manager.authority is not self:
                    raise VerificationFailedError("Wrong workspace authority.")
                binding = self.evidence_completion.verify(conn, spec, evidence_id, worker_id, execution_id, epoch)
                if "worker_log" in binding:
                    output_payload = {"output_hash": binding["worker_log"], "verified_evidence": binding}
                else:
                    output_payload = {**(output_payload or {}), "verified_evidence": binding}

            v_receipt = self.verifier.verify(
                spec,
                effects=effects,
                output_payload=output_payload,
                execution_id=execution_id,
                epoch=current_epoch,
                revision=current_rev,
            )

            if (
                not isinstance(v_receipt, VerificationReceipt)
                or v_receipt.passed is not True
                or v_receipt.task_unique_key != unique_key
                or v_receipt.acceptance_hash != spec.acceptance_hash
                or v_receipt.execution_id != execution_id
                or v_receipt.epoch != current_epoch
                or v_receipt.revision != current_rev
                or not IndependentVerifier.replay_verify(v_receipt, spec, effects=effects, output_payload=output_payload)
            ):
                raise VerificationFailedError(
                    f"VerificationReceipt rejected by controller for '{unique_key}' (passed={getattr(v_receipt, 'passed', None)})."
                )

            # M1.4 Req 1: Re-validate live lease, deadline, execution, epoch, and revision immediately before commit!
            commit_now_dt = self.clock.now_utc()
            commit_now_iso = _format_utc(commit_now_dt)
            self._check_deadline(spec, commit_now_dt)
            self._validate_active_lease(
                conn,
                unique_key=unique_key,
                current_epoch=current_epoch,
                worker_id=worker_id,
                execution_id=execution_id,
                epoch=epoch,
                now_dt=commit_now_dt,
            )

            conn.execute(
                """
                INSERT INTO verification_receipts (
                    verification_id, task_unique_key, acceptance_hash, execution_id,
                    epoch, revision, passed, evidence_hash, evidence_json,
                    verifier_name, verified_at_utc, output_payload_json, effects_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    v_receipt.verification_id,
                    v_receipt.task_unique_key,
                    v_receipt.acceptance_hash,
                    v_receipt.execution_id,
                    v_receipt.epoch,
                    v_receipt.revision,
                    1,
                    v_receipt.evidence_hash,
                    v_receipt.evidence_json,
                    v_receipt.verifier_name,
                    v_receipt.verified_at_utc,
                    json.dumps(output_payload, sort_keys=True, ensure_ascii=False),
                    json.dumps(effects, sort_keys=True, ensure_ascii=False),
                ),
            )

            result_obj = {
                "output_hash": output_payload["output_hash"] if output_payload else None,
                "verification_id": v_receipt.verification_id,
                "verification_evidence_hash": v_receipt.evidence_hash,
            }
            new_rev = current_rev + 1
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, revision = ?, result_json = ?, updated_at_utc = ?
                WHERE unique_key = ? AND revision = ? AND status = 'VERIFYING';
                """,
                (
                    TaskStatus.SUCCEEDED.value,
                    new_rev,
                    json.dumps(result_obj, sort_keys=True, ensure_ascii=False),
                    commit_now_iso,
                    unique_key,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise CASConflictError(f"Concurrent CAS update detected during verify_and_complete on '{unique_key}'.")

            conn.execute("DELETE FROM leases WHERE task_unique_key = ?;", (unique_key,))
            self._append_event_and_outbox(
                conn,
                event_type="TASK_VERIFIED",
                task_unique_key=unique_key,
                execution_id=execution_id,
                epoch=current_epoch,
                payload={
                    "verification_id": v_receipt.verification_id,
                    "evidence_hash": v_receipt.evidence_hash,
                    "verifier_name": v_receipt.verifier_name,
                },
            )
            self._append_event_and_outbox(
                conn,
                event_type="TASK_SUCCEEDED",
                task_unique_key=unique_key,
                execution_id=execution_id,
                epoch=current_epoch,
                payload={
                    "unique_key": unique_key,
                    "from": TaskStatus.VERIFYING.value,
                    "to": TaskStatus.SUCCEEDED.value,
                    "revision": new_rev,
                    "verification_id": v_receipt.verification_id,
                },
            )
            if before_commit_hook is not None:
                before_commit_hook(conn)

            updated = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            return self._task_from_row(updated), v_receipt

    def acquire_lease(self, unique_key: str, worker_id: str, ttl_seconds: int | None = None) -> LeaseRecord:
        """Atomically acquire or steal an expired lease, incrementing fencing_epoch."""
        now_dt = self.clock.now_utc()
        now_iso = _format_utc(now_dt)

        with self.store.transaction() as conn:
            now_dt = self.clock.now_utc()
            now_iso = _format_utc(now_dt)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspaces'").fetchone():
                if conn.execute("SELECT 1 FROM workspaces WHERE task_unique_key = ? AND status IN ('CREATING', 'CLEANING', 'NEEDS_RECONCILIATION')", (unique_key,)).fetchone():
                    raise ReconciliationRequiredError("Workspace creation, cleanup or reconciliation pending.")
            task_row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if task_row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")
            spec = self._spec_from_row(task_row)
            self._check_deadline(spec, now_dt)

            for key in spec.dependencies:
                if not conn.execute(
                    "SELECT 1 FROM tasks WHERE unique_key=? AND project_id=? AND repo_id=? AND status='SUCCEEDED'",
                    (key, spec.project_id, spec.repo_id),
                ).fetchone():
                    raise PolicyDeniedError("Dependency has no successful terminal result.")
            self._check_ancestors(conn, task_row)

            status = TaskStatus(task_row["status"])
            if status == TaskStatus.NEEDS_RECONCILIATION:
                raise ReconciliationRequiredError(
                    f"Task '{unique_key}' is in NEEDS_RECONCILIATION and cannot be leased until reconciled."
                )
            self._assert_operation_allowed("acquire_lease", status, unique_key)

            pending_effects = conn.execute(
                "SELECT COUNT(*) FROM effects WHERE task_unique_key = ? AND outcome IN ('PENDING', 'UNKNOWN');",
                (unique_key,),
            ).fetchone()[0]
            if int(pending_effects) > 0:
                raise ReconciliationRequiredError(
                    f"Task '{unique_key}' has unresolved PENDING/UNKNOWN effects and requires reconciliation."
                )

            existing_lease = conn.execute(
                "SELECT * FROM leases WHERE task_unique_key = ?;", (unique_key,)
            ).fetchone()
            if existing_lease is not None:
                expires_dt = _parse_utc(existing_lease["expires_at_utc"])
                if expires_dt > now_dt:
                    raise LeaseConflictError(
                        f"Task '{unique_key}' already leased by '{existing_lease['worker_id']}' "
                        f"until {existing_lease['expires_at_utc']}."
                    )

            workspace_id = None
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspaces'").fetchone():
                candidates = conn.execute("SELECT workspace_id FROM workspaces WHERE task_unique_key=? AND status='ACTIVE'", (unique_key,)).fetchall()
                if len(candidates) > 1:
                    raise ReconciliationRequiredError("Ambiguous workspace dispatch.")
                if candidates:
                    workspace_id = candidates[0]["workspace_id"]
                    if existing_lease is not None or conn.execute("SELECT 1 FROM workspace_dispatch WHERE workspace_id=?", (workspace_id,)).fetchone():
                        raise ReconciliationRequiredError("Previously dispatched workspace requires reconciliation.")

            lease_ttl = ttl_seconds if ttl_seconds is not None else spec.budget.lease_seconds
            expires_iso = _format_utc(now_dt + timedelta(seconds=lease_ttl))
            new_epoch = int(task_row["fencing_epoch"]) + 1
            execution_id = str(uuid.uuid4())

            conn.execute(
                "UPDATE tasks SET fencing_epoch = ?, updated_at_utc = ? WHERE unique_key = ?;",
                (new_epoch, now_iso, unique_key),
            )
            conn.execute(
                """
                INSERT INTO executions (execution_id, task_unique_key, epoch, worker_id, status, started_at_utc)
                VALUES (?, ?, ?, ?, 'ACTIVE', ?);
                """,
                (execution_id, unique_key, new_epoch, worker_id, now_iso),
            )
            conn.execute(
                """
                INSERT INTO leases (
                    task_unique_key, execution_id, worker_id, fencing_epoch,
                    acquired_at_utc, heartbeat_at_utc, expires_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_unique_key) DO UPDATE SET
                    execution_id = excluded.execution_id,
                    worker_id = excluded.worker_id,
                    fencing_epoch = excluded.fencing_epoch,
                    acquired_at_utc = excluded.acquired_at_utc,
                    heartbeat_at_utc = excluded.heartbeat_at_utc,
                    expires_at_utc = excluded.expires_at_utc;
                """,
                (unique_key, execution_id, worker_id, new_epoch, now_iso, now_iso, expires_iso),
            )
            if workspace_id is not None:
                conn.execute("INSERT INTO workspace_dispatch(workspace_id, execution_id, epoch, worker_id) VALUES (?, ?, ?, ?)", (workspace_id, execution_id, new_epoch, worker_id))
            self._append_event_and_outbox(
                conn,
                event_type="LEASE_ACQUIRED",
                task_unique_key=unique_key,
                execution_id=execution_id,
                epoch=new_epoch,
                payload={"worker_id": worker_id, "epoch": new_epoch, "expires_at_utc": expires_iso},
            )
            return LeaseRecord(
                task_unique_key=unique_key,
                execution_id=execution_id,
                worker_id=worker_id,
                fencing_epoch=new_epoch,
                acquired_at_utc=now_iso,
                heartbeat_at_utc=now_iso,
                expires_at_utc=expires_iso,
            )

    def heartbeat_lease(
        self,
        unique_key: str,
        worker_id: str,
        epoch: int,
        *,
        execution_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> LeaseRecord:
        now_dt = self.clock.now_utc()
        now_iso = _format_utc(now_dt)

        with self.store.transaction() as conn:
            task_row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if task_row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")
            self._assert_operation_allowed("heartbeat_lease", TaskStatus(task_row["status"]), unique_key)
            lease_row = conn.execute("SELECT * FROM leases WHERE task_unique_key = ?;", (unique_key,)).fetchone()
            if (
                lease_row is None
                or int(lease_row["fencing_epoch"]) != epoch
                or lease_row["worker_id"] != worker_id
                or (execution_id is not None and lease_row["execution_id"] != execution_id)
            ):
                raise StaleEpochError(f"Stale heartbeat for '{unique_key}' (worker={worker_id}, epoch={epoch}).")
            if _parse_utc(lease_row["expires_at_utc"]) <= now_dt:
                raise LeaseExpiredError(f"Lease for '{unique_key}' expired at {lease_row['expires_at_utc']}.")

            spec = self._spec_from_row(task_row)
            self._check_deadline(spec, now_dt)
            lease_ttl = ttl_seconds if ttl_seconds is not None else spec.budget.lease_seconds
            new_expires = _format_utc(now_dt + timedelta(seconds=lease_ttl))
            conn.execute(
                "UPDATE leases SET heartbeat_at_utc = ?, expires_at_utc = ? WHERE task_unique_key = ?;",
                (now_iso, new_expires, unique_key),
            )
            return LeaseRecord(
                task_unique_key=unique_key,
                execution_id=lease_row["execution_id"],
                worker_id=worker_id,
                fencing_epoch=epoch,
                acquired_at_utc=lease_row["acquired_at_utc"],
                heartbeat_at_utc=now_iso,
                expires_at_utc=new_expires,
            )

    def record_tool_intent(
        self,
        unique_key: str,
        *,
        worker_id: str,
        execution_id: str,
        epoch: int,
        tool: str,
        args: dict[str, Any],
        operation_id: str = "op-0",
        target_path: str | None = None,
        approval_ref: str = "local-policy",
        broker_capability: object | None = None,
    ) -> ToolIntentSubmission:
        """Persist ToolIntent deterministically; enforce max_steps, deadline, and tool-independent idempotency."""
        now_dt = self.clock.now_utc()
        now_iso = _format_utc(now_dt)

        with self.store.transaction() as conn:
            task_row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if task_row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")
            self._assert_operation_allowed("record_tool_intent", TaskStatus(task_row["status"]), unique_key)
            spec = self._spec_from_row(task_row)
            self._check_deadline(spec, now_dt)
            self.policy_gate.check_tool(spec, tool, args=args, target_path=target_path)

            self._validate_active_lease(
                conn,
                unique_key=unique_key,
                current_epoch=int(task_row["fencing_epoch"]),
                worker_id=worker_id,
                execution_id=execution_id,
                epoch=epoch,
                now_dt=now_dt,
            )

            if tool == "sandbox.python" and broker_capability is not self._broker_capability:
                raise PolicyDeniedError("Native execution requires broker authorization.")

            args_hash = canonical_json_hash(args)
            # M1.4: Idempotency key excludes `tool` so changing tool on same operation_id cannot bypass idempotency!
            idempotency_key = canonical_json_hash(
                {
                    "task_unique_key": unique_key,
                    "operation_id": operation_id,
                }
            )
            existing = conn.execute(
                "SELECT * FROM effects WHERE idempotency_key = ?;", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["args_hash"] != args_hash or existing["tool"] != tool:
                    raise TaskConflictError(
                        f"Idempotency key '{operation_id}' conflict on '{unique_key}': args or tool mismatch."
                    )
                existing_outcome = EffectOutcome(existing["outcome"])
                if existing_outcome == EffectOutcome.SUCCEEDED:
                    # Record safe reuse across retry execution so verify_and_complete can verify settled effect
                    conn.execute(
                        "INSERT OR IGNORE INTO effect_reuses (execution_id, intent_id, reused_at_utc) VALUES (?, ?, ?);",
                        (execution_id, existing["intent_id"], now_iso),
                    )
                    disposition = IntentDisposition.SETTLED
                elif existing_outcome == EffectOutcome.FAILED:
                    disposition = IntentDisposition.SETTLED
                else:
                    disposition = IntentDisposition.PENDING_RECONCILIATION
                return ToolIntentSubmission(
                    intent=ToolIntent(
                        intent_id=existing["intent_id"],
                        execution_id=existing["execution_id"],
                        epoch=int(existing["epoch"]),
                        tool=existing["tool"],
                        args_hash=existing["args_hash"],
                        approval_ref=existing["approval_ref"],
                        idempotency_key=existing["idempotency_key"],
                    ),
                    outcome=existing_outcome,
                    disposition=disposition,
                )

            if tool == "sandbox.python":
                occupied = conn.execute(
                    "SELECT COUNT(*) FROM effects WHERE tool = ? AND outcome IN ('PENDING', 'UNKNOWN')", (tool,)
                ).fetchone()[0]
                if occupied >= 2:
                    raise BudgetExceededError("Both native worker slots are reserved.")

            current_steps = int(task_row["step_count"]) if "step_count" in task_row.keys() else 0
            reserved = self._reserved_child_steps(conn, spec)
            if current_steps + reserved >= spec.budget.max_steps:
                raise BudgetExceededError(
                    f"Step budget exhausted for '{unique_key}': {current_steps} >= {spec.budget.max_steps}."
                )

            intent_id = str(uuid.uuid4())
            intent = ToolIntent(
                intent_id=intent_id,
                execution_id=execution_id,
                epoch=epoch,
                tool=tool,
                args_hash=args_hash,
                approval_ref=approval_ref,
                idempotency_key=idempotency_key,
            )
            conn.execute(
                "UPDATE tasks SET step_count = step_count + 1, updated_at_utc = ? WHERE unique_key = ?;",
                (now_iso, unique_key),
            )
            conn.execute(
                """
                INSERT INTO effects (
                    intent_id, task_unique_key, execution_id, epoch, tool,
                    args_hash, approval_ref, idempotency_key, outcome,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    intent.intent_id,
                    unique_key,
                    execution_id,
                    epoch,
                    tool,
                    args_hash,
                    approval_ref,
                    idempotency_key,
                    EffectOutcome.PENDING.value,
                    now_iso,
                    now_iso,
                ),
            )
            self._append_event_and_outbox(
                conn,
                event_type="TOOL_INTENT_RECORDED",
                task_unique_key=unique_key,
                execution_id=execution_id,
                epoch=epoch,
                payload={
                    "intent_id": intent.intent_id,
                    "tool": tool,
                    "operation_id": operation_id,
                    "idempotency_key": idempotency_key,
                },
            )
            return ToolIntentSubmission(
                intent=intent,
                outcome=EffectOutcome.PENDING,
                disposition=IntentDisposition.CREATED,
            )

    def record_effect_receipt(
        self,
        unique_key: str,
        receipt: EffectReceipt,
        *,
        worker_id: str,
        epoch: int,
        execution_id: str | None = None,
        broker_capability: object | None = None,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> TaskRecord:
        """Atomically record EffectReceipt and (when UNKNOWN) transition to NEEDS_RECONCILIATION."""
        if receipt.outcome == EffectOutcome.PENDING:
            raise InvalidTransitionError("Receipt outcome cannot revert to PENDING.")

        now_dt = self.clock.now_utc()
        now_iso = _format_utc(now_dt)

        with self.store.transaction() as conn:
            task_row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if task_row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")
            self._assert_operation_allowed("record_effect_receipt", TaskStatus(task_row["status"]), unique_key)

            lease_row = self._validate_active_lease(
                conn,
                unique_key=unique_key,
                current_epoch=int(task_row["fencing_epoch"]),
                worker_id=worker_id,
                execution_id=execution_id,
                epoch=epoch,
                now_dt=now_dt,
            )

            eff_row = conn.execute(
                "SELECT * FROM effects WHERE intent_id = ?;", (receipt.intent_id,)
            ).fetchone()
            if eff_row is None:
                raise TaskNotFoundError(f"Effect intent '{receipt.intent_id}' does not exist.")
            if eff_row["tool"] == "sandbox.python" and broker_capability is not self._broker_capability:
                raise PolicyDeniedError("Only the native broker can settle its effects.")
            if eff_row["task_unique_key"] != unique_key:
                raise TaskConflictError(
                    f"Effect '{receipt.intent_id}' belongs to '{eff_row['task_unique_key']}', not '{unique_key}'."
                )
            if eff_row["execution_id"] != lease_row["execution_id"] or int(eff_row["epoch"]) != epoch:
                raise StaleEpochError(
                    f"Effect '{receipt.intent_id}' execution/epoch mismatch "
                    f"({eff_row['execution_id']}/{eff_row['epoch']} != {lease_row['execution_id']}/{epoch})."
                )
            if eff_row["outcome"] != EffectOutcome.PENDING.value:
                raise InvalidTransitionError(
                    f"Effect '{receipt.intent_id}' already settled as '{eff_row['outcome']}'."
                )

            cursor = conn.execute(
                """
                UPDATE effects
                SET outcome = ?, external_ref = ?, evidence_hash = ?, updated_at_utc = ?
                WHERE intent_id = ? AND task_unique_key = ? AND execution_id = ? AND epoch = ? AND outcome = 'PENDING';
                """,
                (
                    receipt.outcome.value,
                    receipt.external_ref,
                    receipt.evidence_hash,
                    now_iso,
                    receipt.intent_id,
                    unique_key,
                    lease_row["execution_id"],
                    epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise DatabaseIntegrityError(
                    f"Failed to update effect '{receipt.intent_id}' atomically (rowcount={cursor.rowcount})."
                )

            if eff_row["tool"] == "sandbox.python":
                from opencognitiveos.verification.seals import seal
                gate = self.evidence_completion
                if gate is None:
                    raise VerificationFailedError("Missing native broker signing context.")
                conn.execute("""CREATE TABLE IF NOT EXISTS broker_seals (
                    intent TEXT PRIMARY KEY REFERENCES effects(intent_id), tag TEXT NOT NULL)""")
                digest = canonical_json_hash([unique_key, execution_id, epoch, receipt.intent_id,
                    eff_row["args_hash"], eff_row["approval_ref"], receipt.outcome.value, receipt.external_ref, receipt.evidence_hash])
                conn.execute("INSERT INTO broker_seals VALUES (?,?)",
                             (receipt.intent_id, seal(gate.verifier_key, "effect", digest)))

            self._append_event_and_outbox(
                conn,
                event_type="EFFECT_RECEIPT_RECORDED",
                task_unique_key=unique_key,
                execution_id=lease_row["execution_id"],
                epoch=epoch,
                payload={
                    "intent_id": receipt.intent_id,
                    "outcome": receipt.outcome.value,
                    "external_ref": receipt.external_ref,
                    "evidence_hash": receipt.evidence_hash,
                },
            )

            if receipt.outcome == EffectOutcome.UNKNOWN:
                current_status = TaskStatus(task_row["status"])
                if TaskStatus.NEEDS_RECONCILIATION not in ALLOWED_TRANSITIONS[current_status]:
                    raise InvalidTransitionError(
                        f"Cannot transition '{unique_key}' from {current_status.value} to NEEDS_RECONCILIATION."
                    )
                cur_rev = int(task_row["revision"])
                new_rev = cur_rev + 1
                err_msg = f"External effect '{receipt.intent_id}' outcome is UNKNOWN; reconciliation required."
                t_cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, revision = ?, error_message = ?, updated_at_utc = ?
                    WHERE unique_key = ? AND revision = ?;
                    """,
                    (
                        TaskStatus.NEEDS_RECONCILIATION.value,
                        new_rev,
                        err_msg,
                        now_iso,
                        unique_key,
                        cur_rev,
                    ),
                )
                if t_cursor.rowcount != 1:
                    raise CASConflictError(f"Concurrent CAS update during reconciliation transition on '{unique_key}'.")
                conn.execute("DELETE FROM leases WHERE task_unique_key = ?;", (unique_key,))
                self._append_event_and_outbox(
                    conn,
                    event_type="TASK_NEEDS_RECONCILIATION",
                    task_unique_key=unique_key,
                    execution_id=lease_row["execution_id"],
                    epoch=epoch,
                    payload={
                        "unique_key": unique_key,
                        "from": current_status.value,
                        "to": TaskStatus.NEEDS_RECONCILIATION.value,
                        "revision": new_rev,
                        "epoch": epoch,
                    },
                )

            if before_commit_hook is not None:
                before_commit_hook(conn)

            updated = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            return self._task_from_row(updated)

    def reconcile_effect(
        self,
        unique_key: str,
        *,
        intent_id: str,
        resolved_outcome: EffectOutcome,
        external_ref: str | None,
        evidence_hash: str,
    ) -> TaskRecord:
        """Resolve a PENDING/UNKNOWN effect and transition NEEDS_RECONCILIATION -> VERIFYING or FAILED."""
        if resolved_outcome not in {EffectOutcome.SUCCEEDED, EffectOutcome.FAILED}:
            raise ValueError("resolved_outcome must be SUCCEEDED or FAILED.")
        now_iso = self.clock.now_iso()

        with self.store.transaction() as conn:
            task_row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if task_row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")
            if TaskStatus(task_row["status"]) != TaskStatus.NEEDS_RECONCILIATION:
                raise InvalidTransitionError(f"Task '{unique_key}' is not in NEEDS_RECONCILIATION.")

            eff_row = conn.execute("SELECT * FROM effects WHERE intent_id = ?;", (intent_id,)).fetchone()
            if eff_row is None or eff_row["task_unique_key"] != unique_key:
                raise TaskNotFoundError(f"Effect '{intent_id}' not found on task '{unique_key}'.")
            if eff_row["tool"] == "sandbox.python":
                raise PolicyDeniedError("Native execution needs independently verified quiescence.")
            if eff_row["outcome"] not in {EffectOutcome.PENDING.value, EffectOutcome.UNKNOWN.value}:
                raise InvalidTransitionError(f"Effect '{intent_id}' is already settled ({eff_row['outcome']}).")

            cursor = conn.execute(
                """
                UPDATE effects
                SET outcome = ?, external_ref = ?, evidence_hash = ?, updated_at_utc = ?
                WHERE intent_id = ? AND task_unique_key = ? AND outcome IN ('PENDING', 'UNKNOWN');
                """,
                (resolved_outcome.value, external_ref, evidence_hash, now_iso, intent_id, unique_key),
            )
            if cursor.rowcount != 1:
                raise DatabaseIntegrityError(f"Failed to reconcile effect '{intent_id}'.")

            remaining = int(
                conn.execute(
                    "SELECT COUNT(*) FROM effects WHERE task_unique_key = ? AND outcome IN ('PENDING', 'UNKNOWN');",
                    (unique_key,),
                ).fetchone()[0]
            )
            self._append_event_and_outbox(
                conn,
                event_type="EFFECT_RECONCILED",
                task_unique_key=unique_key,
                execution_id="reconciler",
                epoch=int(task_row["fencing_epoch"]),
                payload={
                    "intent_id": intent_id,
                    "resolved_outcome": resolved_outcome.value,
                    "external_ref": external_ref,
                    "remaining_unresolved": remaining,
                },
            )

            if resolved_outcome == EffectOutcome.FAILED:
                target = TaskStatus.FAILED
            elif remaining == 0:
                target = TaskStatus.VERIFYING
            else:
                updated = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
                return self._task_from_row(updated)

            cur_rev = int(task_row["revision"])
            new_rev = cur_rev + 1
            err_msg = None if target == TaskStatus.VERIFYING else f"Reconciled effect {intent_id} failed."
            t_cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, revision = ?, error_message = ?, updated_at_utc = ?
                WHERE unique_key = ? AND revision = ?;
                """,
                (target.value, new_rev, err_msg, now_iso, unique_key, cur_rev),
            )
            if t_cursor.rowcount != 1:
                raise CASConflictError(f"Concurrent CAS update during reconcile on '{unique_key}'.")

            self._append_event_and_outbox(
                conn,
                event_type=f"TASK_{target.value}",
                task_unique_key=unique_key,
                execution_id="reconciler",
                epoch=int(task_row["fencing_epoch"]),
                payload={
                    "unique_key": unique_key,
                    "from": TaskStatus.NEEDS_RECONCILIATION.value,
                    "to": target.value,
                    "revision": new_rev,
                },
            )
            updated = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            return self._task_from_row(updated)

    def schedule_retry(self, unique_key: str, *, expected_revision: int, reason: str) -> TaskRecord:
        """Atomically check retry budget, increment retry_count, transition to READY, and emit outbox."""
        now_iso = self.clock.now_iso()

        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"Task '{unique_key}' not found.")
            current_status = TaskStatus(row["status"])
            current_rev = int(row["revision"])
            current_epoch = int(row["fencing_epoch"])
            retry_count = int(row["retry_count"])
            spec = self._spec_from_row(row)

            if current_status not in {TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.RETRY_WAIT}:
                raise InvalidTransitionError(
                    f"Cannot schedule_retry from status {current_status.value} on '{unique_key}'."
                )
            if current_rev != expected_revision:
                raise CASConflictError(
                    f"CAS revision mismatch on retry for '{unique_key}': expected {expected_revision}, actual {current_rev}."
                )
            if retry_count >= spec.budget.max_retries:
                raise BudgetExceededError(
                    f"Retry budget exhausted for '{unique_key}': {retry_count} >= {spec.budget.max_retries}."
                )

            unresolved = int(
                conn.execute(
                    "SELECT COUNT(*) FROM effects WHERE task_unique_key = ? AND outcome IN ('PENDING', 'UNKNOWN');",
                    (unique_key,),
                ).fetchone()[0]
            )
            if unresolved > 0:
                raise ReconciliationRequiredError(
                    f"Task '{unique_key}' has {unresolved} PENDING/UNKNOWN effect(s); must reconcile before retry."
                )

            new_rev = current_rev + 1
            new_retry = retry_count + 1
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, revision = ?, retry_count = ?, error_message = ?, updated_at_utc = ?
                WHERE unique_key = ? AND revision = ?;
                """,
                (TaskStatus.READY.value, new_rev, new_retry, reason, now_iso, unique_key, expected_revision),
            )
            if cursor.rowcount != 1:
                raise CASConflictError(f"Concurrent CAS update detected on schedule_retry for '{unique_key}'.")

            conn.execute("DELETE FROM leases WHERE task_unique_key = ?;", (unique_key,))
            self._append_event_and_outbox(
                conn,
                event_type="TASK_RETRIED_TO_READY",
                task_unique_key=unique_key,
                execution_id="controller",
                epoch=current_epoch,
                payload={
                    "unique_key": unique_key,
                    "from": current_status.value,
                    "to": TaskStatus.READY.value,
                    "retry_count": new_retry,
                    "revision": new_rev,
                    "reason": reason,
                },
            )
            updated = conn.execute("SELECT * FROM tasks WHERE unique_key = ?;", (unique_key,)).fetchone()
            return self._task_from_row(updated)

    def cancel_task(
        self, unique_key: str, *, expected_revision: int, reason: str = "User requested cancellation"
    ) -> TaskRecord:
        return self.transition_task(
            unique_key,
            TaskStatus.CANCELLED,
            expected_revision=expected_revision,
            actor_role="controller", controller_capability=self._controller_capability,
            error_message=reason,
        )

    def dispatch_outbox(
        self,
        consumer_name: str,
        handler: Callable[[dict[str, Any]], None],
        *,
        include_dispatched: bool = False,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> int:
        """At-least-once outbox delivery with per-consumer deduplication (no external exactly-once claim)."""
        delivered = 0
        with self.store.open_connection() as conn:
            query = (
                "SELECT * FROM outbox ORDER BY outbox_id ASC;"
                if include_dispatched
                else "SELECT * FROM outbox WHERE status = 'PENDING' ORDER BY outbox_id ASC;"
            )
            rows = conn.execute(query).fetchall()

        for row in rows:
            event_id = row["event_id"]
            try:
                with self.store.transaction() as conn:
                    already = conn.execute(
                        "SELECT 1 FROM outbox_consumptions WHERE consumer_name = ? AND event_id = ?;",
                        (consumer_name, event_id),
                    ).fetchone()
                    if already is None:
                        handler(
                            {
                                "event_id": event_id,
                                "task_unique_key": row["task_unique_key"],
                                "topic": row["topic"],
                                "payload": json.loads(row["payload_json"]),
                            }
                        )
                        conn.execute(
                            "INSERT INTO outbox_consumptions (consumer_name, event_id, consumed_at_utc) VALUES (?, ?, ?);",
                            (consumer_name, event_id, self.clock.now_iso()),
                        )
                        delivered += 1
                    conn.execute(
                        """
                        UPDATE outbox
                        SET status = 'DISPATCHED',
                            delivery_attempts = delivery_attempts + 1,
                            dispatched_at_utc = ?,
                            last_error = NULL
                        WHERE outbox_id = ?;
                        """,
                        (self.clock.now_iso(), row["outbox_id"]),
                    )
                    if before_commit_hook is not None:
                        before_commit_hook(conn)
            except Exception as exc:
                with self.store.transaction() as err_conn:
                    err_conn.execute(
                        """
                        UPDATE outbox
                        SET delivery_attempts = delivery_attempts + 1,
                            last_error = ?
                        WHERE outbox_id = ?;
                        """,
                        (str(exc), row["outbox_id"]),
                    )
                raise
        return delivered

    def recover_on_restart(self) -> dict[str, Any]:
        """Recover all non-terminal persisted states after restart without relying on memory."""
        now_dt = self.clock.now_utc()
        recovered_to_ready: list[str] = []
        moved_to_reconciliation: list[str] = []
        failed_budget_exhausted: list[str] = []

        with self.store.open_connection() as conn:
            active_rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?, ?);",
                (TaskStatus.RUNNING.value, TaskStatus.VERIFYING.value, TaskStatus.RETRY_WAIT.value),
            ).fetchall()
            waiting_children = [
                r["unique_key"]
                for r in conn.execute(
                    "SELECT unique_key FROM tasks WHERE status = ?;", (TaskStatus.WAITING_CHILDREN.value,)
                ).fetchall()
            ]
            waiting_approval = [
                r["unique_key"]
                for r in conn.execute(
                    "SELECT unique_key FROM tasks WHERE status = ?;", (TaskStatus.WAITING_APPROVAL.value,)
                ).fetchall()
            ]

        for row in active_rows:
            task = self._task_from_row(row)
            with self.store.open_connection() as conn:
                unresolved = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM effects WHERE task_unique_key = ? AND outcome IN ('PENDING', 'UNKNOWN');",
                        (task.unique_key,),
                    ).fetchone()[0]
                )
                lease = conn.execute(
                    "SELECT * FROM leases WHERE task_unique_key = ?;", (task.unique_key,)
                ).fetchone()

            if unresolved > 0:
                if task.status == TaskStatus.RETRY_WAIT:
                    self.transition_task(
                        task.unique_key,
                        TaskStatus.FAILED,
                        expected_revision=task.revision,
                        actor_role="controller", controller_capability=self._controller_capability,
                        error_message="RETRY_WAIT task had unresolved PENDING/UNKNOWN effects on restart.",
                    )
                    failed_budget_exhausted.append(task.unique_key)
                else:
                    now_iso = _format_utc(now_dt)
                    err_msg = f"Found {unresolved} PENDING/UNKNOWN effect(s) during restart recovery."
                    with self.store.transaction() as t_conn:
                        cur_rev = task.revision
                        new_rev = cur_rev + 1
                        cur = t_conn.execute(
                            """
                            UPDATE tasks
                            SET status = ?, revision = ?, error_message = ?, updated_at_utc = ?
                            WHERE unique_key = ? AND revision = ?;
                            """,
                            (TaskStatus.NEEDS_RECONCILIATION.value, new_rev, err_msg, now_iso, task.unique_key, cur_rev),
                        )
                        if cur.rowcount != 1:
                            raise CASConflictError(f"Concurrent CAS update during recovery on '{task.unique_key}'.")
                        t_conn.execute("DELETE FROM leases WHERE task_unique_key = ?;", (task.unique_key,))
                        self._append_event_and_outbox(
                            t_conn,
                            event_type="TASK_NEEDS_RECONCILIATION",
                            task_unique_key=task.unique_key,
                            execution_id="recovery",
                            epoch=task.fencing_epoch,
                            payload={
                                "unique_key": task.unique_key,
                                "from": task.status.value,
                                "to": TaskStatus.NEEDS_RECONCILIATION.value,
                                "revision": new_rev,
                                "unresolved_effects": unresolved,
                            },
                        )
                    moved_to_reconciliation.append(task.unique_key)
                continue

            if task.status == TaskStatus.RETRY_WAIT:
                if task.retry_count < task.spec.budget.max_retries:
                    self.schedule_retry(
                        task.unique_key,
                        expected_revision=task.revision,
                        reason="Recovered RETRY_WAIT task to READY after restart.",
                    )
                    recovered_to_ready.append(task.unique_key)
                else:
                    self.transition_task(
                        task.unique_key,
                        TaskStatus.FAILED,
                        expected_revision=task.revision,
                        actor_role="controller", controller_capability=self._controller_capability,
                        error_message="RETRY_WAIT budget exhausted on restart.",
                    )
                    failed_budget_exhausted.append(task.unique_key)
                continue

            expired = lease is None or _parse_utc(lease["expires_at_utc"]) <= now_dt
            if expired:
                if task.retry_count < task.spec.budget.max_retries:
                    self.schedule_retry(
                        task.unique_key,
                        expected_revision=task.revision,
                        reason=f"Recovered {task.status.value} task after restart with expired/missing lease.",
                    )
                    recovered_to_ready.append(task.unique_key)
                else:
                    self.transition_task(
                        task.unique_key,
                        TaskStatus.FAILED,
                        expected_revision=task.revision,
                        actor_role="controller", controller_capability=self._controller_capability,
                        error_message="Lease expired on restart and retry budget exhausted.",
                    )
                    failed_budget_exhausted.append(task.unique_key)

        with self.store.open_connection() as conn:
            pending_outbox = int(
                conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'PENDING';").fetchone()[0]
            )
            reconcile_rows = conn.execute(
                "SELECT unique_key FROM tasks WHERE status = ?;", (TaskStatus.NEEDS_RECONCILIATION.value,)
            ).fetchall()

        return {
            "recovered_to_ready": recovered_to_ready,
            "moved_to_reconciliation": moved_to_reconciliation,
            "failed_budget_exhausted": failed_budget_exhausted,
            "waiting_children_pending_m5": waiting_children,
            "waiting_approval_pending": waiting_approval,
            "pending_outbox": pending_outbox,
            "needs_reconciliation": [r["unique_key"] for r in reconcile_rows],
        }
