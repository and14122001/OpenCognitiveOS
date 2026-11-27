"""Single-controller inference slot, restart-safe handoff and conservative usage accounting."""
import json
import re
from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.errors import BudgetExceededError, PolicyDeniedError


class InferenceCoordinator:
    def __init__(self, authority, *, controller_capability):
        if controller_capability is None:
            raise ValueError("Missing inference supervisor capability.")
        self.auth = authority
        self._control = controller_capability
        with self.auth.store.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS inference_allocations (
                task TEXT PRIMARY KEY REFERENCES tasks(unique_key), token_limit INTEGER NOT NULL,
                model TEXT NOT NULL, tokenizer TEXT NOT NULL, policy_hash TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS inference_calls (
                reservation TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES inference_allocations(task),
                execution_id TEXT NOT NULL, epoch INTEGER NOT NULL, tokens INTEGER NOT NULL,
                used INTEGER, snapshot TEXT NOT NULL, snapshot_hash TEXT NOT NULL, state TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS inference_traces (
                reservation TEXT PRIMARY KEY REFERENCES inference_calls(reservation),
                input_sha256 TEXT NOT NULL, output_sha256 TEXT NOT NULL)""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_inference
                ON inference_calls(state) WHERE state='ACTIVE'""")

            conn.execute("""CREATE TABLE IF NOT EXISTS inference_releases (
                task TEXT PRIMARY KEY REFERENCES inference_allocations(task),
                generation INTEGER NOT NULL, config_hash TEXT NOT NULL)""")

    def authorize_supervisor(self, capability):
        if capability is not self._control:
            raise PolicyDeniedError("Inference requires the configured supervisor.")

    def allocate(self, task, model, tokenizer, policy_hash, tokens, *, capability):
        if (capability is not self._control or type(tokens) is not int or tokens <= 0
                or not model or not tokenizer or not policy_hash):
            raise PolicyDeniedError("Missing trusted inference allocation.")
        with self.auth.store.transaction() as conn:
            task_row = conn.execute("SELECT parent_id FROM tasks WHERE unique_key=?", (task,)).fetchone()
            if task_row is None or task_row["parent_id"] is not None:
                raise PolicyDeniedError("A shared token allocation belongs to the root task.")
            conn.execute("INSERT INTO inference_allocations VALUES (?,?,?,?,?)",
                         (task, tokens, model, tokenizer, policy_hash))

    def allocate_current(self, task, policy_hash, tokens, *, capability):
        self.authorize_supervisor(capability)
        if type(tokens) is not int or tokens <= 0 or not policy_hash:
            raise PolicyDeniedError("Missing trusted allocation limits.")
        with self.auth.store.transaction() as conn:
            task_row = conn.execute("SELECT parent_id FROM tasks WHERE unique_key=?", (task,)).fetchone()
            if task_row is None or task_row["parent_id"] is not None:
                raise PolicyDeniedError("Release allocations require a root task.")
            row = conn.execute("SELECT * FROM model_releases ORDER BY generation DESC LIMIT 1").fetchone()
            if row is None:
                raise PolicyDeniedError("No approved release.")
            cfg = json.loads(row["config"])
            if canonical_json_hash(cfg) != row["config_hash"]:
                raise PolicyDeniedError("Active release digest mismatch.")
            conn.execute("INSERT INTO inference_allocations VALUES (?,?,?,?,?)",
                (task,tokens,cfg["model"],cfg["tokenizer"],policy_hash))
            conn.execute("INSERT INTO inference_releases VALUES (?,?,?)",(task,row["generation"],row["config_hash"]))

    def begin(self, task, *, worker_id, execution_id, epoch, request_id, max_tokens, provider=None):
        if not request_id or type(max_tokens) is not int or max_tokens <= 0:
            raise PolicyDeniedError("Invalid inference reservation.")
        with self.auth.store.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE unique_key=?", (task,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise PolicyDeniedError("Inactive inference task.")
            self.auth._validate_active_lease(conn, unique_key=task, current_epoch=row["fencing_epoch"],
                    worker_id=worker_id, execution_id=execution_id, epoch=epoch, now_dt=self.auth.clock.now_utc())
            spec = self.auth._spec_from_row(row)
            if provider is not None and provider not in spec.allowed_providers:
                raise PolicyDeniedError("Local provider is outside the task grant.")
            root = row
            while root["parent_id"] is not None:
                root = conn.execute("SELECT * FROM tasks WHERE repo_id=? AND project_id=? AND task_id=?",
                                    (root["repo_id"], root["project_id"], root["parent_id"])).fetchone()
            allocation = conn.execute("SELECT * FROM inference_allocations WHERE task=?", (root["unique_key"],)).fetchone()
            if allocation is None:
                raise PolicyDeniedError("No root token allocation.")
            pin = conn.execute("SELECT generation,config_hash FROM inference_releases WHERE task=?",
                               (root["unique_key"],)).fetchone()
            if pin is not None:
                release = conn.execute("SELECT generation,config_hash FROM model_releases ORDER BY generation DESC LIMIT 1").fetchone()
                if release is None or tuple(release) != tuple(pin):
                    raise PolicyDeniedError("Release changed; a fresh controller allocation is required.")
            rid = canonical_json_hash([task, execution_id, epoch, request_id])
            if conn.execute("SELECT 1 FROM inference_calls WHERE reservation=?", (rid,)).fetchone():
                raise PolicyDeniedError("Replay cannot start a second inference call.")
            if conn.execute("SELECT 1 FROM inference_calls WHERE state='ACTIVE'").fetchone():
                raise BudgetExceededError("Inference slot still occupied.")
            spent = conn.execute("SELECT COALESCE(SUM(COALESCE(used,tokens)),0) FROM inference_calls WHERE task=?",
                                 (root["unique_key"],)).fetchone()[0]
            if spent + max_tokens > allocation["token_limit"]:
                raise BudgetExceededError("Shared token budget exhausted.")
            # Only controller-derived provenance enters the handoff; no arbitrary context or credential values.
            snapshot = dict(task_key=task, root=root["unique_key"], execution_id=execution_id, epoch=epoch,
                            base_sha=spec.base_sha, acceptance_hash=spec.acceptance_hash,
                            policy_hash=allocation["policy_hash"], model_id=allocation["model"],
                            tokenizer_version=allocation["tokenizer"], budget_reservation_id=rid,
                            max_tokens=max_tokens, data_class=spec.data_class.value)
            if provider is not None:
                snapshot["provider"] = provider
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_attachments'").fetchone():
                media = []
                for item in conn.execute("SELECT * FROM local_attachments WHERE task=? ORDER BY id", (task,)):
                    envelope = json.loads(item["envelope"])
                    gate = self.auth.evidence_completion
                    if (gate is None or canonical_json_hash(envelope) != item["id"]
                            or envelope["task_hash"] != spec.content_hash() or envelope["task"] != task
                            or envelope["sha256"] != item["digest"] or not gate.cas.verify_digest(item["digest"])):
                        raise PolicyDeniedError("Local input context failed verification.")
                    media.append(item["id"])
                if media:
                    snapshot["local_attachments"] = media
            if pin is not None:
                snapshot["release_generation"], snapshot["config_hash"] = tuple(pin)
            conn.execute("INSERT INTO inference_calls VALUES (?,?,?,?,?,NULL,?,?,'ACTIVE')",
                         (rid, root["unique_key"], execution_id, epoch, max_tokens, json.dumps(snapshot),
                          canonical_json_hash(snapshot)))
            return rid, snapshot

    def settle(self, reservation, actual_tokens, *, capability, trace=None):
        """Only the transport supervisor confirms exit. Unknown usage consumes the reservation."""
        if capability is not self._control:
            raise PolicyDeniedError("Only the trusted supervisor can release the inference slot.")
        with self.auth.store.transaction() as conn:
            row = conn.execute("SELECT * FROM inference_calls WHERE reservation=?", (reservation,)).fetchone()
            if row is None or row["state"] != "ACTIVE":
                raise PolicyDeniedError("No matching live reservation.")
            used = row["tokens"] if actual_tokens is None else actual_tokens
            if type(used) is not int or used < 0 or used > row["tokens"]:
                raise BudgetExceededError("Unreconciled usage; retain occupied slot.")
            if trace is not None:
                if (not isinstance(trace, tuple) or len(trace) != 2 or actual_tokens is None
                        or not all(isinstance(d, str) and re.fullmatch(r"[a-f0-9]{64}", d) for d in trace)):
                    raise PolicyDeniedError("Confirmed usage and two content digests required.")
                conn.execute("INSERT INTO inference_traces VALUES (?,?,?)", (reservation, *trace))
            conn.execute("UPDATE inference_calls SET used=?,state='CLOSED' WHERE reservation=?", (used, reservation))
