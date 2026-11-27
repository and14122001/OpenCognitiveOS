"""Stage immutable child outputs for a separate review. This operation never applies a patch."""
import json
import re

from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.errors import PolicyDeniedError, VerificationFailedError


def stage_candidates(authority, root, base_sha, proposals, *, capability):
    gate = authority.evidence_completion
    if gate is None or capability is not authority._controller_capability:
        raise PolicyDeniedError("Candidate staging requires the evidence controller.")
    if not re.fullmatch("[0-9a-f]{40}", base_sha) or not 1 <= len(proposals) <= 3:
        raise PolicyDeniedError("Expected a pinned base and at most three child outputs.")
    with authority.store.transaction() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE unique_key=?", (root,)).fetchone()
        if row is None or row["status"] in ("FAILED", "CANCELLED", "SUCCEEDED"):
            raise PolicyDeniedError("Parent cannot receive candidates.")
        parent = authority._spec_from_row(row)
        if parent.base_sha != base_sha:
            raise PolicyDeniedError("Parent revision changed.")
        files = {}
        for child, path in sorted(proposals):
            entry = conn.execute("SELECT * FROM tasks WHERE unique_key=?", (child,)).fetchone()
            if entry is None or entry["status"] != "SUCCEEDED":
                raise PolicyDeniedError("Child output unavailable.")
            spec = authority._spec_from_row(entry)
            if ((spec.project_id, spec.repo_id, spec.parent_id, spec.base_sha) !=
                    (parent.project_id, parent.repo_id, parent.task_id, base_sha)):
                raise PolicyDeniedError("Child provenance differs from parent.")
            parts = authority.policy_gate._validate_relative_path(path, spec.allowed_paths)
            if path != "/".join(parts) or not all(re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9._-]*", p) for p in parts):
                raise PolicyDeniedError("Candidate path must be canonical.")
            for other in files:
                a, b = path.casefold(), other.casefold()
                if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                    raise PolicyDeniedError("Candidate file conflict.")
            receipt = conn.execute("""SELECT verification_id,output_payload_json FROM verification_receipts
                WHERE task_unique_key=? AND verifier_name='independent-native-evidence-v1'""", (child,)).fetchone()
            if receipt is None or not authority.replay_verification(child):
                raise VerificationFailedError("Native, replayable receipt required for every child.")
            binding = json.loads(receipt["output_payload_json"])["verified_evidence"]
            if not binding.get("worker_log"):
                raise VerificationFailedError("Missing immutable candidate bytes.")
            files[path] = dict(child=child, receipt=receipt["verification_id"], manifest=binding["manifest_id"],
                               sha256=binding["worker_log"])
        bundle = dict(parent=root, base_sha=base_sha, acceptance_hash=parent.acceptance_hash, files=files)
        digest = canonical_json_hash(bundle)
        conn.execute("""CREATE TABLE IF NOT EXISTS candidate_bundles (
            digest TEXT PRIMARY KEY, root TEXT NOT NULL REFERENCES tasks(unique_key), bundle TEXT NOT NULL)""")
        inserted = conn.execute("INSERT OR IGNORE INTO candidate_bundles VALUES (?,?,?)",
                                (digest, root, json.dumps(bundle, sort_keys=True)))
        if inserted.rowcount:
            authority._append_event_and_outbox(conn, event_type="CANDIDATES_STAGED", task_unique_key=root,
                execution_id="controller", epoch=row["fencing_epoch"], payload={"digest": digest})
        return digest, bundle
