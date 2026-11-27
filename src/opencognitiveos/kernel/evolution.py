"""Controller-authorized local model configuration revisions and compare-and-swap rollback."""
import json
from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.errors import CASConflictError, PolicyDeniedError
from opencognitiveos.verification.benchmark import compare


class EvolutionLab:
    def __init__(self, store, *, controller_capability):
        if controller_capability is None:
            raise ValueError("Missing release authority.")
        self.store = store
        self._control = controller_capability
        with store.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS model_releases (
                generation INTEGER PRIMARY KEY, config_hash TEXT NOT NULL, config TEXT NOT NULL,
                previous_generation INTEGER REFERENCES model_releases(generation),
                operation TEXT NOT NULL, evidence_hash TEXT NOT NULL)""")

    def current(self):
        with self.store.open_connection() as conn:
            row = conn.execute("SELECT * FROM model_releases ORDER BY generation DESC LIMIT 1").fetchone()
        if row is None:
            return None
        config = json.loads(row["config"])
        if canonical_json_hash(config) != row["config_hash"]:
            raise PolicyDeniedError("Release configuration integrity failure.")
        return row["generation"], config

    def _commit(self, expected, config, operation, evidence_hash, capability):
        if capability is not self._control:
            raise PolicyDeniedError("Release changes require independent controller approval.")
        if (set(config) != {"model", "tokenizer", "strategy"}
                or config["strategy"] not in ("single", "long-thinking", "independent", "hierarchical", "flat", "dynamic")
                or not all(isinstance(v, str) and 0 < len(v) <= 256 for v in config.values())):
            raise PolicyDeniedError("Unknown model configuration.")
        with self.store.transaction() as conn:
            current = conn.execute("SELECT COALESCE(MAX(generation),0) FROM model_releases").fetchone()[0]
            if current != expected:
                raise CASConflictError("Release head changed.")
            conn.execute("INSERT INTO model_releases VALUES (?,?,?,?,?,?)",
                         (current + 1, canonical_json_hash(config), json.dumps(config), current or None,
                          operation, evidence_hash))
            return current + 1

    def initialize(self, config, *, capability):
        if config.get("strategy") != "single":
            raise PolicyDeniedError("Initial baseline requires a single agent.")
        return self._commit(0, config, "BASELINE", canonical_json_hash(config), capability)

    def promote(self, expected, candidate, *, plan_path, plan_sha256, observations, capability):
        if capability is not self._control:
            raise PolicyDeniedError("Release approval missing.")
        active = self.current()
        if active is None or active[0] != expected:
            raise CASConflictError("Unknown baseline revision.")
        # The release authority supplies independently verified observations and the preregistered digest.
        from pathlib import Path
        import hashlib
        raw = Path(plan_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != plan_sha256:
            raise PolicyDeniedError("Preregistration digest mismatch.")
        plan = json.loads(raw)
        baseline = active[1]
        if (plan["model"] != baseline["model"] or candidate["model"] != baseline["model"]
                or candidate["tokenizer"] != baseline["tokenizer"]
                or plan["baseline"] != baseline["strategy"] or plan["candidate"] != candidate["strategy"]):
            raise PolicyDeniedError("Trial arms do not match release configurations.")
        report = compare(plan_path, plan_sha256, observations)
        if not report["eligible_for_review"]:
            raise PolicyDeniedError("No verified improvement or safety regression.")
        evidence = canonical_json_hash({"report": report, "observations": observations})
        return self._commit(expected, candidate, "PROMOTE", evidence, capability)

    def rollback(self, expected, target, *, capability):
        if type(target) is not int or not 0 < target < expected:
            raise PolicyDeniedError("Rollback target must be a previous generation.")
        with self.store.open_connection() as conn:
            row = conn.execute("SELECT * FROM model_releases WHERE generation=?", (target,)).fetchone()
        if row is None:
            raise PolicyDeniedError("Unknown rollback target.")
        config = json.loads(row["config"])
        if canonical_json_hash(config) != row["config_hash"]:
            raise PolicyDeniedError("Rollback configuration integrity failure.")
        return self._commit(expected, config, "ROLLBACK", canonical_json_hash({"target": target}), capability)
