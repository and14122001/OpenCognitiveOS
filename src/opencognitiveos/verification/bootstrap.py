"""Compose a trusted controller from operator-owned POSIX configuration and external keys."""
import json
import os
import stat
from pathlib import Path

from opencognitiveos.errors import PolicyDeniedError
from opencognitiveos.execution.worktree import WorkspaceManager
from opencognitiveos.verification.completion import EvidenceCompletion
from opencognitiveos.verification.evidence_store import ContentAddressedEvidenceStore


def _private_read(path):
    path = Path(path)
    if os.name != "posix" or not path.is_absolute():
        raise PolicyDeniedError("Trusted configuration requires an absolute path and verifiable POSIX ownership.")
    if os.path.realpath(path) != str(path):
        raise PolicyDeniedError("Use a canonical path to the trusted file.")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as fh:
        info = os.fstat(fh.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > 65536):
            raise PolicyDeniedError("Untrusted owner, file type, access mode or configuration size.")
        raw = fh.read(65537)
        if len(raw) > 65536:
            raise PolicyDeniedError("Configuration size exceeded.")
        return raw


def configure_evidence(authority, config_path):
    config = json.loads(_private_read(config_path))
    repo, workspaces, cas = [Path(config[key]).resolve(strict=True) for key in ("repo", "workspaces", "cas")]
    secret_paths = [Path(config[key]) for key in ("controller_key_file", "verifier_key_file")]
    private = [Path(config_path), *secret_paths, authority.store.db_path.resolve(), cas]
    if any(path.is_relative_to(root) for path in private for root in (repo, workspaces)):
        raise PolicyDeniedError("Controller state and keys must be outside the repository and workspaces.")
    keys = [_private_read(path) for path in secret_paths]
    if keys[0] == keys[1] or any(len(key) != 32 for key in keys):
        raise PolicyDeniedError("Two independent 32-byte keys are required.")
    policy = ContentAddressedEvidenceStore.validate_digest(config["policy_hash"])
    manager = WorkspaceManager(authority=authority, repo_path=repo, workspaces_root=workspaces)
    authority.evidence_completion = EvidenceCompletion(manager, ContentAddressedEvidenceStore(cas), *keys, policy)
