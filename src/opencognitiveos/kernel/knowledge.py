"""Append-only, task-scoped evidence relationships. Assertions preserve contradictory evidence."""
from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.errors import PolicyDeniedError, VerificationFailedError


class KnowledgeGraph:
    def __init__(self, authority, *, controller_capability):
        if controller_capability is None:
            raise ValueError("Missing evidence curator capability.")
        self.auth = authority
        self._control = controller_capability
        with authority.store.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_edges (
                seq INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE, project TEXT NOT NULL, repo TEXT NOT NULL,
                subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL, task TEXT NOT NULL,
                verification_id TEXT NOT NULL REFERENCES verification_receipts(verification_id),
                evidence_hash TEXT NOT NULL, observed_at TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_subject ON knowledge_edges(project,repo,subject,seq)")

    def append(self, *, task, subject, relation, target, capability):
        if capability is not self._control or relation not in {"supports", "contradicts", "derived_from", "supersedes"}:
            raise PolicyDeniedError("Unsupported or unauthorized evidence relationship.")
        if not all(isinstance(x, str) and 0 < len(x.encode()) <= 256 for x in (subject, target)):
            raise ValueError("Bounded evidence identifiers required.")
        with self.auth.store.transaction() as conn:
            row = conn.execute("""SELECT t.project_id,t.repo_id,v.verification_id,v.evidence_hash
                FROM tasks t JOIN verification_receipts v ON t.unique_key=v.task_unique_key
                WHERE t.unique_key=? AND t.status='SUCCEEDED'""", (task,)).fetchone()
            if row is None or not self.auth.replay_verification(task):
                raise VerificationFailedError("An independently replayable success receipt is required.")
            fields = (row["project_id"], row["repo_id"], subject, relation, target, task,
                      row["verification_id"], row["evidence_hash"])
            digest = canonical_json_hash(fields)
            conn.execute("""INSERT OR IGNORE INTO knowledge_edges
                (digest,project,repo,subject,relation,object,task,verification_id,evidence_hash,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (digest, *fields, self.auth.clock.now_iso()))
        return digest

    def history(self, *, project, repo, subject, through_sequence=None):
        with self.auth.store.open_connection() as conn:
            rows = conn.execute("""SELECT * FROM knowledge_edges WHERE project=? AND repo=? AND subject=?
                AND (? IS NULL OR seq<=?) ORDER BY seq""", (project, repo, subject, through_sequence, through_sequence))
            return [dict(row) for row in rows]
