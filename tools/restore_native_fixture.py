"""Verify a Linux-produced public test fixture on a fresh host. No operator secrets are used."""
import hashlib
from pathlib import Path
import sys
import tempfile

from opencognitiveos.contracts.models import TaskStatus, canonical_json_hash
from opencognitiveos.execution.worktree import WorkspaceManager
from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.storage.evidence_backup import restore_snapshot
from opencognitiveos.storage.sqlite_store import SQLiteStore
from opencognitiveos.verification.completion import EvidenceCompletion
from opencognitiveos.verification.evidence_store import ContentAddressedEvidenceStore


def public_fixture_keys():
    return tuple(hashlib.sha256(b"PUBLIC CI FIXTURE ONLY/" + label).digest()
                 for label in (b"controller", b"verifier"))


def main(source, digest):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        dest = root / "restored"
        restore_snapshot(source, dest, digest)
        store = SQLiteStore(dest / "state.sqlite3")
        auth = ExecutionAuthority(store)
        manager = WorkspaceManager(authority=auth, repo_path=Path.cwd(), workspaces_root=root / "workspaces")
        auth.evidence_completion = EvidenceCompletion(manager, ContentAddressedEvidenceStore(dest / "cas"),
            *public_fixture_keys(), canonical_json_hash({"net": "deny"}))
        if (auth.get_task("r::t").status != TaskStatus.SUCCEEDED or not auth.replay_verification("r::t")
                or not auth.audit_database_consistency("r::t")["ok"]):
            raise RuntimeError("Restored receipt or database consistency check failed.")
        print("Restored Linux SQLite/CAS and independently replayed native SUCCEEDED receipt:", digest)


if __name__ == "__main__":
    main(*sys.argv[1:])
