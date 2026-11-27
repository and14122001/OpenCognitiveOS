"""A task-scoped software world model grounded in immutable Git blobs."""
import hashlib
import hmac
import json

from opencognitiveos.errors import PolicyDeniedError, VerificationFailedError
from opencognitiveos.verification.acceptance import AcceptanceSpecRegistry
from opencognitiveos.verification.evidence_store import EvidenceKind
from opencognitiveos.verification.seals import spec_seal


def capture_world(authority, task_key, paths, *, capability):
    gate = authority.evidence_completion
    if gate is None or capability is not authority._controller_capability:
        raise PolicyDeniedError("World capture requires the evidence controller.")
    if not 1 <= len(paths) <= 64 or len(set(paths)) != len(paths):
        raise PolicyDeniedError("Expected distinct source paths within the 64-file inventory limit.")
    task = authority.get_task(task_key).spec
    acceptance = AcceptanceSpecRegistry(authority)
    _, spec_hash = acceptance.load_and_verify_spec(task_spec=task, expected_policy_hash=gate.policy_hash)
    with authority.store.open_connection() as conn:
        tag = conn.execute("SELECT tag FROM acceptance_seals WHERE digest=?", (spec_hash,)).fetchone()
        if not tag or not hmac.compare_digest(tag[0], spec_seal(gate.controller_key, spec_hash, task)):
            raise VerificationFailedError("Source inventory lacks trusted task authorization.")
    manager = gate.manager
    tree = manager._run_git("rev-parse", "--verify", task.base_sha + "^{tree}").stdout.decode("ascii").strip()
    files = {}
    size_sum = 0
    for path in sorted(paths):
        parts = authority.policy_gate._validate_relative_path(path, task.allowed_paths)
        if path != "/".join(parts) or len(path.encode()) > 512:
            raise PolicyDeniedError("Source path must be canonical and bounded.")
        raw = manager._run_git("--literal-pathspecs", "ls-tree", "-z", task.base_sha, "--", path).stdout
        record, sep, name = raw.rstrip(b"\0").partition(b"\t")
        fields = record.split()
        if (not sep or name != path.encode() or len(fields) != 3 or fields[1] != b"blob"
                or fields[0] not in (b"100644", b"100755")):
            raise PolicyDeniedError("Expected a regular, tracked source file.")
        oid = fields[2].decode("ascii")
        size = int(manager._run_git("cat-file", "-s", oid).stdout)
        size_sum += size
        if size > 1048576 or size_sum > 8388608:
            raise PolicyDeniedError("Source inventory exceeds byte limit.")
        data = manager._run_git("cat-file", "blob", oid).stdout
        if len(data) != size:
            raise VerificationFailedError("Git blob size mismatch.")
        files[path] = {"git_blob": oid, "mode": fields[0].decode(), "bytes":size,
                       "sha256":hashlib.sha256(data).hexdigest()}
    world = {"version":1, "task":task_key, "task_hash":task.content_hash(), "base_sha":task.base_sha,
             "tree":tree, "spec_hash":spec_hash, "files":files}
    with authority.store.transaction() as conn:
        tag = conn.execute("SELECT tag FROM acceptance_seals WHERE digest=?", (spec_hash,)).fetchone()
        if not tag or not hmac.compare_digest(tag[0], spec_seal(gate.controller_key, spec_hash, task)):
            raise VerificationFailedError("Source inventory lacks trusted task authorization.")
        raw = json.dumps(world, separators=(",", ":"), sort_keys=True).encode()
        digest = gate.cas.store_bytes(raw, kind=EvidenceKind.RAW_EVIDENCE).digest
        conn.execute("""CREATE TABLE IF NOT EXISTS world_snapshots (
            digest TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(unique_key), base_sha TEXT NOT NULL)""")
        added = conn.execute("INSERT OR IGNORE INTO world_snapshots VALUES (?,?,?)", (digest, task_key, task.base_sha))
        if added.rowcount:
            authority._append_event_and_outbox(conn, event_type="SOURCE_WORLD_CAPTURED", task_unique_key=task_key,
                    execution_id="controller", epoch=0, payload={"digest":digest, "base_sha":task.base_sha})
    return digest, world
