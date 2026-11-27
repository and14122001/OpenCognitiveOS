"""Local controller message routing for sibling tasks. Messages carry no execution authority."""
import hashlib
from datetime import timedelta

from opencognitiveos.errors import BudgetExceededError, CASConflictError, PolicyDeniedError
from opencognitiveos.kernel.authority import ExecutionAuthority, _format_utc


class TaskMessages:
    def __init__(self, authority: ExecutionAuthority, *, controller_capability):
        if controller_capability is None:
            raise ValueError("Missing topology capability.")
        self.auth = authority
        self._control = controller_capability
        with authority.store.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS message_groups (
                root TEXT PRIMARY KEY REFERENCES tasks(unique_key), version INTEGER NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS message_routes (
                root TEXT REFERENCES message_groups(root), sender TEXT REFERENCES tasks(unique_key),
                recipient TEXT REFERENCES tasks(unique_key), PRIMARY KEY(root,sender,recipient))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS task_messages (
                root TEXT NOT NULL, version INTEGER NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                execution_id TEXT NOT NULL, epoch INTEGER NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
                digest TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL,
                PRIMARY KEY(root,sender,execution_id,recipient,sequence))""")

    def replace_routes(self, root, expected_version, routes, *, capability):
        if capability is not self._control:
            raise PolicyDeniedError("Topology update requires controller authority.")
        if not isinstance(routes, (list, tuple)) or len(routes) > 6:
            raise PolicyDeniedError("Topology exceeds three sibling tasks.")
        with self.auth.store.transaction() as conn:
            parent = conn.execute("SELECT * FROM tasks WHERE unique_key=?", (root,)).fetchone()
            if parent is None or parent["status"] in ("CANCELLED", "FAILED", "SUCCEEDED"):
                raise PolicyDeniedError("Inactive group parent.")
            conn.execute("INSERT OR IGNORE INTO message_groups VALUES (?,0)", (root,))
            if conn.execute("UPDATE message_groups SET version=version+1 WHERE root=? AND version=?",
                            (root, expected_version)).rowcount != 1:
                raise CASConflictError("Stale topology version.")
            conn.execute("DELETE FROM message_routes WHERE root=?", (root,))
            for sender, recipient in routes:
                count = conn.execute("""SELECT COUNT(*) FROM tasks WHERE unique_key IN (?,?)
                    AND parent_id=? AND repo_id=? AND project_id=? AND status NOT IN ('FAILED','CANCELLED','SUCCEEDED')""",
                    (sender, recipient, parent["task_id"], parent["repo_id"], parent["project_id"])).fetchone()[0]
                if count != 2:
                    raise PolicyDeniedError("Routes require two distinct live siblings.")
                conn.execute("INSERT INTO message_routes VALUES (?,?,?)", (root, sender, recipient))
            return expected_version + 1

    def _authorize(self, conn, task, worker_id, execution_id, epoch):
        row = conn.execute("SELECT * FROM tasks WHERE unique_key=?", (task,)).fetchone()
        if row is None or row["status"] != "RUNNING":
            raise PolicyDeniedError("Messaging requires a running task.")
        self.auth._validate_active_lease(conn, unique_key=task, current_epoch=row["fencing_epoch"],
                worker_id=worker_id, execution_id=execution_id, epoch=epoch, now_dt=self.auth.clock.now_utc())

    def publish(self, *, root, version, sender, recipient, kind, body, sequence, worker_id, execution_id, epoch):
        if kind not in ("hypothesis", "evidence", "counterexample", "proposal", "request"):
            raise PolicyDeniedError("Unknown message kind.")
        if not isinstance(body, str) or not 0 < len(body) <= 4096 or type(sequence) is not int or sequence < 1:
            raise PolicyDeniedError("Invalid message.")
        raw = body.encode()
        if len(raw) > 4096:
            raise BudgetExceededError("Message exceeds byte budget.")
        digest = hashlib.sha256(raw).hexdigest()
        with self.auth.store.transaction() as conn:
            self._authorize(conn, sender, worker_id, execution_id, epoch)
            route = conn.execute("""SELECT 1 FROM message_routes r JOIN message_groups g USING(root)
                JOIN tasks t ON t.unique_key=r.recipient WHERE r.root=? AND version=? AND sender=? AND recipient=?
                AND t.status NOT IN ('CANCELLED','FAILED','SUCCEEDED')""", (root, version, sender, recipient)).fetchone()
            if route is None:
                raise PolicyDeniedError("Route revoked or unavailable.")
            used, last = conn.execute("""SELECT COUNT(*),MAX(CASE WHEN sender=? AND execution_id=? AND recipient=?
                THEN sequence END) FROM task_messages WHERE root=?""", (sender, execution_id, recipient, root)).fetchone()
            if sequence != (last or 0) + 1:
                raise CASConflictError("Repeated or out-of-order message.")
            if used >= 64:
                raise BudgetExceededError("Group message budget exhausted.")
            now = self.auth.clock.now_utc()
            conn.execute("INSERT INTO task_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                root, version, sender, recipient, execution_id, epoch, sequence, kind, digest, body,
                _format_utc(now), _format_utc(now + timedelta(minutes=5))))
            self.auth._append_event_and_outbox(conn, event_type="MESSAGE_RECORDED", task_unique_key=sender,
                    execution_id=execution_id, epoch=epoch, payload={"recipient": recipient, "digest": digest,
                                                                    "kind": kind, "sequence": sequence})
        return digest

    def inbox(self, *, root, recipient, worker_id, execution_id, epoch):
        with self.auth.store.transaction() as conn:
            self._authorize(conn, recipient, worker_id, execution_id, epoch)
            rows = conn.execute("""SELECT m.* FROM task_messages m JOIN message_groups g
                ON g.root=m.root AND g.version=m.version WHERE m.root=? AND m.recipient=? AND m.expires>?
                ORDER BY m.rowid""", (root, recipient, _format_utc(self.auth.clock.now_utc())))
            result = []
            for row in rows:
                if hashlib.sha256(row["body"].encode()).hexdigest() != row["digest"]:
                    raise PolicyDeniedError("Corrupt message.")
                result.append(dict(row))
            return result
