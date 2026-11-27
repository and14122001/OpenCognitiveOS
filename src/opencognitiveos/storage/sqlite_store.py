"""SQLite persistence with WAL, foreign keys, migrations, explicit connection close, and backups."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from opencognitiveos.contracts.models import Clock, SystemClock
from opencognitiveos.errors import DatabaseIntegrityError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="001_initial_schema",
        sql="""
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            unique_key TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            repo_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            parent_id TEXT,
            content_hash TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            fencing_epoch INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            spec_json TEXT NOT NULL,
            result_json TEXT,
            error_message TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS dependencies (
            task_unique_key TEXT NOT NULL,
            depends_on_unique_key TEXT NOT NULL,
            PRIMARY KEY (task_unique_key, depends_on_unique_key),
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE,
            FOREIGN KEY (depends_on_unique_key) REFERENCES tasks(unique_key) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS executions (
            execution_id TEXT PRIMARY KEY,
            task_unique_key TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS leases (
            task_unique_key TEXT PRIMARY KEY,
            execution_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            fencing_epoch INTEGER NOT NULL,
            acquired_at_utc TEXT NOT NULL,
            heartbeat_at_utc TEXT NOT NULL,
            expires_at_utc TEXT NOT NULL,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE,
            FOREIGN KEY (execution_id) REFERENCES executions(execution_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS approvals (
            approval_ref TEXT PRIMARY KEY,
            task_unique_key TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            approved_at_utc TEXT NOT NULL,
            expires_at_utc TEXT,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            task_unique_key TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            causation_id TEXT,
            correlation_id TEXT NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            payload_ref TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            task_unique_key TEXT NOT NULL,
            topic TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            dispatched_at_utc TEXT,
            FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS effects (
            intent_id TEXT PRIMARY KEY,
            task_unique_key TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            tool TEXT NOT NULL,
            args_hash TEXT NOT NULL,
            approval_ref TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL,
            external_ref TEXT,
            evidence_hash TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE,
            FOREIGN KEY (execution_id) REFERENCES executions(execution_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            task_unique_key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            media_type TEXT NOT NULL,
            path_or_ref TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS provider_runs (
            run_id TEXT PRIMARY KEY,
            task_unique_key TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            provider_name TEXT NOT NULL,
            is_cloud INTEGER NOT NULL,
            status TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            response_hash TEXT,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS world_observations (
            observation_id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            observed_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS predictions (
            prediction_id TEXT PRIMARY KEY,
            observation_id TEXT,
            horizon INTEGER NOT NULL,
            predicted_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (observation_id) REFERENCES world_observations(observation_id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS knowledge (
            knowledge_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        """,
    ),
    Migration(
        version=2,
        name="002_outbox_consumers_and_indexes",
        sql="""
        CREATE TABLE IF NOT EXISTS outbox_consumptions (
            consumer_name TEXT NOT NULL,
            event_id TEXT NOT NULL,
            consumed_at_utc TEXT NOT NULL,
            PRIMARY KEY (consumer_name, event_id),
            FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, outbox_id);
        CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_unique_key, sequence);
        CREATE INDEX IF NOT EXISTS idx_effects_outcome ON effects(outcome);
        """,
    ),
    Migration(
        version=3,
        name="003_outbox_last_error_and_effect_operation_key",
        sql="""
        ALTER TABLE outbox ADD COLUMN last_error TEXT;
        """,
    ),
    Migration(
        version=4,
        name="004_verification_receipts",
        sql="""
        CREATE TABLE IF NOT EXISTS verification_receipts (
            verification_id TEXT PRIMARY KEY,
            task_unique_key TEXT NOT NULL,
            acceptance_hash TEXT NOT NULL,
            execution_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            passed INTEGER NOT NULL,
            evidence_hash TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            verifier_name TEXT NOT NULL,
            verified_at_utc TEXT NOT NULL,
            FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE,
            FOREIGN KEY (execution_id) REFERENCES executions(execution_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_verification_task ON verification_receipts(task_unique_key, revision);
        """,
    ),
    Migration(
        version=5,
        name="005_verification_replay_evidence",
        sql="""
        ALTER TABLE verification_receipts ADD COLUMN output_payload_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE verification_receipts ADD COLUMN effects_snapshot_json TEXT NOT NULL DEFAULT '[]';
        """,
    ),
    Migration(
        version=6,
        name="006_step_count_and_effect_reuses",
        sql="""
        ALTER TABLE tasks ADD COLUMN step_count INTEGER NOT NULL DEFAULT 0;
        CREATE TABLE IF NOT EXISTS effect_reuses (
            execution_id TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            reused_at_utc TEXT NOT NULL,
            PRIMARY KEY (execution_id, intent_id),
            FOREIGN KEY (execution_id) REFERENCES executions(execution_id) ON DELETE CASCADE,
            FOREIGN KEY (intent_id) REFERENCES effects(intent_id) ON DELETE CASCADE
        );
        """,
    ),
)


class SQLiteStore:
    """Manages SQLite connections, pragmas, migrations, transactions, and backups."""

    def __init__(self, db_path: str | Path, clock: Clock | None = None, busy_timeout_ms: int = 5000) -> None:
        self.db_path = Path(db_path)
        self.clock: Clock = clock or SystemClock()
        self.busy_timeout_ms = busy_timeout_ms
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Create a raw connection. Callers MUST close it (prefer `open_connection()`)."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)};")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    @contextmanager
    def open_connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a SQLite connection and guarantee conn.close() on exit."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        owns_conn = conn is None
        active = conn or self.connect()
        try:
            active.execute("BEGIN IMMEDIATE;")
            yield active
            active.execute("COMMIT;")
        except Exception:
            try:
                active.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise
        finally:
            if owns_conn:
                active.close()

    def migrate(self) -> list[int]:
        applied_now: list[int] = []
        with self.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at_utc TEXT NOT NULL,
                    checksum TEXT NOT NULL
                );
                """
            )
            existing = {
                row["version"]: row["checksum"]
                for row in conn.execute("SELECT version, checksum FROM schema_migrations;").fetchall()
            }
            for migration in MIGRATIONS:
                if migration.version in existing:
                    if existing[migration.version] != migration.checksum:
                        raise DatabaseIntegrityError(
                            f"Migration {migration.version} ({migration.name}) checksum mismatch."
                        )
                    continue
                for stmt in (s.strip() for s in migration.sql.split(";") if s.strip()):
                    conn.execute(stmt + ";")
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at_utc, checksum) VALUES (?, ?, ?, ?);",
                    (migration.version, migration.name, self.clock.now_iso(), migration.checksum),
                )
                applied_now.append(migration.version)
        return applied_now

    def schema_version(self) -> int:
        with self.open_connection() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
            ).fetchone()
            if row is None:
                return 0
            max_row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations;").fetchone()
            return int(max_row["v"]) if max_row else 0

    def integrity_check(self) -> dict[str, object]:
        with self.open_connection() as conn:
            integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check;").fetchall()]
            fk_rows = [dict(row) for row in conn.execute("PRAGMA foreign_key_check;").fetchall()]
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            fk_enabled = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        ok = integrity_rows == ["ok"] and len(fk_rows) == 0 and int(fk_enabled) == 1
        result = {
            "ok": ok,
            "integrity_check": integrity_rows,
            "foreign_key_check": fk_rows,
            "journal_mode": journal_mode,
            "foreign_keys": int(fk_enabled),
        }
        if not ok:
            raise DatabaseIntegrityError(f"SQLite integrity check failed: {result}")
        return result

    def backup_to(self, destination_path: str | Path) -> Path:
        dest = Path(destination_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.open_connection() as src_conn:
            with closing(sqlite3.connect(str(dest))) as dst_conn:
                src_conn.backup(dst_conn)
        return dest

    @classmethod
    def restore_from(cls, backup_path: str | Path, target_path: str | Path, clock: Clock | None = None) -> SQLiteStore:
        src = Path(backup_path)
        dst = Path(target_path)
        source_uri = src.resolve(strict=True).as_uri() + "?mode=ro"
        if src.resolve() == dst.resolve():
            raise DatabaseIntegrityError("Backup and destination must be different files.")
        with closing(sqlite3.connect(source_uri, uri=True)) as src_conn:
            # Validate the same read snapshot that backup() will copy.
            src_conn.execute("BEGIN")
            tables = {row[0] for row in src_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"tasks", "leases", "verification_receipts"}.issubset(tables):
                raise DatabaseIntegrityError("Backup lacks required OpenCognitiveOS tables.")
            if src_conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise DatabaseIntegrityError("Backup failed SQLite integrity_check.")
            if src_conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise DatabaseIntegrityError("Backup contains invalid foreign keys.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(str(dst))) as dst_conn:
                src_conn.backup(dst_conn)
        store = cls(dst, clock=clock)
        store.integrity_check()
        return store
