"""S29: Git SHA & Execution Environment Binding (EvidenceManifest).

Security Boundary Notice:
- SHA-256 hash integrity verifies deterministic consistency across AcceptanceSpec,
  Git state, execution environment, and content-addressed evidence; it does NOT
  prove origin authenticity and cannot defend against an attacker with full
  controller and SQLite database write access.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencognitiveos.contracts.models import (
    Clock,
    TaskSpec,
    canonical_json_hash,
)
from opencognitiveos.errors import (
    LeaseExpiredError,
    StaleEpochError,
    VerificationFailedError,
)
if TYPE_CHECKING:
    from opencognitiveos.kernel.authority import ExecutionAuthority
from opencognitiveos.verification.acceptance import AcceptanceSpecRegistry
from opencognitiveos.verification.evidence_store import (
    ContentAddressedEvidenceStore,
    EvidenceKind,
)

EVIDENCE_MANIFEST_SCHEMA_VERSION = "1.0.0"


def __canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _controlled_git_env() -> dict[str, str]:
    git_bin = shutil.which("git")
    parts = [str(Path(git_bin).parent)] if git_bin else []
    for d in ("/usr/bin", "/bin"):
        if d not in parts and Path(d).is_dir():
            parts.append(d)
    env = {
        "PATH": os.pathsep.join(parts),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "0",
    }
    for win_k in ("SYSTEMROOT", "COMSPEC", "PATHEXT"):
        if win_k in os.environ:
            env[win_k] = os.environ[win_k]
    return env


def _run_controlled_git(cwd: Path, *args: str) -> bytes:
    git_bin = shutil.which("git")
    if not git_bin:
        raise VerificationFailedError("git executable not found for trusted Git SHA capture.")
    res = subprocess.run(
        [
            git_bin,
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "advice.detachedHead=false",
            "-C",
            str(cwd),
            *args,
        ],
        env=_controlled_git_env(),
        capture_output=True,
        timeout=15.0,
        check=False,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise VerificationFailedError(f"Trusted git {' '.join(args)} failed: {err}")
    return res.stdout


@dataclass(frozen=True)
class TrustedGitState:
    base_sha: str
    tested_commit_sha: str
    tested_tree_sha: str
    dirty_workspace: bool
    uncommitted_snapshot_hash: str | None


def capture_trusted_git_state(
    worktree_path: Path,
    *,
    base_sha: str,
    evidence_store: ContentAddressedEvidenceStore,
    claimed_commit_sha: str | None = None,
    claimed_clean: bool | None = None,
) -> TrustedGitState:
    """Capture Git commit/tree SHA and dirty snapshot directly from controller/verifier."""
    wt = Path(worktree_path).resolve(strict=True)
    commit_sha = _run_controlled_git(wt, "rev-parse", "HEAD").decode("utf-8").strip()
    tree_sha = _run_controlled_git(wt, "rev-parse", "HEAD^{tree}").decode("utf-8").strip()

    if claimed_commit_sha is not None and claimed_commit_sha != commit_sha:
        raise VerificationFailedError(
            f"Forged Git commit SHA claimed ({claimed_commit_sha}) != actual ({commit_sha})."
        )

    status_raw = _run_controlled_git(wt, "status", "--porcelain", "-z")
    dirty = len(status_raw) > 0
    if claimed_clean is True and dirty:
        raise VerificationFailedError(
            "Workspace has uncommitted changes; cannot claim clean HEAD SHA."
        )

    snapshot_hash: str | None = None
    if dirty:
        diff_bytes = _run_controlled_git(wt, "diff", "HEAD")
        untracked_raw = _run_controlled_git(wt, "ls-files", "--others", "--exclude-standard", "-z")
        untracked_entries: list[dict[str, str]] = []
        for item in sorted(x.decode("utf-8", "replace") for x in untracked_raw.split(b"\x00") if x):
            file_path = wt / item
            if file_path.is_file() and not file_path.is_symlink():
                digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
                untracked_entries.append({"path": item, "sha256": digest})
        snapshot_payload = _canonical_json(
            {
                "commit_sha": commit_sha,
                "tree_sha": tree_sha,
                "status_porcelain_hex": status_raw.hex(),
                "diff_head_sha256": hashlib.sha256(diff_bytes).hexdigest(),
                "untracked": untracked_entries,
            }
        ).encode("utf-8")
        stored = evidence_store.store_bytes(
            snapshot_payload,
            kind=EvidenceKind.RAW_EVIDENCE,
            actor_role="verifier",
        )
        snapshot_hash = stored.digest

    return TrustedGitState(
        base_sha=base_sha,
        tested_commit_sha=commit_sha,
        tested_tree_sha=tree_sha,
        dirty_workspace=dirty,
        uncommitted_snapshot_hash=snapshot_hash,
    )


@dataclass(frozen=True)
class ExecutionEnvironmentRecord:
    """Secret-free execution environment & test run summary."""

    os_name: str
    os_release: str
    os_version: str
    cpu_arch: str
    python_version: str
    git_version: str
    sandbox_backend: str
    sandbox_version: str
    dependency_lock_hash: str
    test_command: str
    exit_code: int
    passed_count: int
    failed_count: int
    skipped_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "os_name": self.os_name,
            "os_release": self.os_release,
            "os_version": self.os_version,
            "cpu_arch": self.cpu_arch,
            "python_version": self.python_version,
            "git_version": self.git_version,
            "sandbox_backend": self.sandbox_backend,
            "sandbox_version": self.sandbox_version,
            "dependency_lock_hash": self.dependency_lock_hash,
            "test_command": self.test_command,
            "exit_code": self.exit_code,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
        }

    def environment_hash(self) -> str:
        return canonical_json_hash(self.to_dict())

    @classmethod
    def capture(
        cls,
        *,
        repo_root: Path,
        sandbox_backend: str,
        sandbox_version: str,
        test_command: str,
        exit_code: int,
        passed_count: int,
        failed_count: int,
        skipped_count: int,
    ) -> ExecutionEnvironmentRecord:
        git_ver = _run_controlled_git(repo_root, "--version").decode("utf-8", "replace").strip()
        pyproject = repo_root / "pyproject.toml"
        lock_hash = (
            hashlib.sha256(pyproject.read_bytes()).hexdigest()
            if pyproject.is_file()
            else canonical_json_hash({"stdlib_only": True})
        )
        return cls(
            os_name=platform.system(),
            os_release=platform.release(),
            os_version=platform.version(),
            cpu_arch=platform.machine(),
            python_version=platform.python_version(),
            git_version=git_ver,
            sandbox_backend=sandbox_backend,
            sandbox_version=sandbox_version,
            dependency_lock_hash=lock_hash,
            test_command=test_command,
            exit_code=int(exit_code),
            passed_count=int(passed_count),
            failed_count=int(failed_count),
            skipped_count=int(skipped_count),
        )


@dataclass(frozen=True)
class EvidenceManifest:
    schema_version: str
    evidence_id: str
    project_id: str
    repo_id: str
    task_unique_key: str
    execution_id: str
    worker_id: str
    fencing_epoch: int
    workspace_id: str
    base_sha: str
    tested_commit_sha: str
    tested_tree_sha: str
    acceptance_hash: str
    policy_hash: str
    environment_hash: str
    artifact_hashes: dict[str, str]
    verifier_id: str
    created_at_utc: str
    dirty_workspace: bool = False
    uncommitted_snapshot_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "project_id": self.project_id,
            "repo_id": self.repo_id,
            "task_unique_key": self.task_unique_key,
            "execution_id": self.execution_id,
            "worker_id": self.worker_id,
            "fencing_epoch": self.fencing_epoch,
            "workspace_id": self.workspace_id,
            "base_sha": self.base_sha,
            "tested_commit_sha": self.tested_commit_sha,
            "tested_tree_sha": self.tested_tree_sha,
            "dirty_workspace": self.dirty_workspace,
            "uncommitted_snapshot_hash": self.uncommitted_snapshot_hash,
            "acceptance_hash": self.acceptance_hash,
            "policy_hash": self.policy_hash,
            "environment_hash": self.environment_hash,
            "artifact_hashes": dict(sorted(self.artifact_hashes.items())),
            "verifier_id": self.verifier_id,
            "created_at_utc": self.created_at_utc,
        }

    def manifest_hash(self) -> str:
        return canonical_json_hash(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceManifest:
        return cls(
            schema_version=str(data["schema_version"]),
            evidence_id=str(data["evidence_id"]),
            project_id=str(data["project_id"]),
            repo_id=str(data["repo_id"]),
            task_unique_key=str(data["task_unique_key"]),
            execution_id=str(data["execution_id"]),
            worker_id=str(data["worker_id"]),
            fencing_epoch=int(data["fencing_epoch"]),
            workspace_id=str(data["workspace_id"]),
            base_sha=str(data["base_sha"]),
            tested_commit_sha=str(data["tested_commit_sha"]),
            tested_tree_sha=str(data["tested_tree_sha"]),
            acceptance_hash=str(data["acceptance_hash"]),
            policy_hash=str(data["policy_hash"]),
            environment_hash=str(data["environment_hash"]),
            artifact_hashes=dict(data["artifact_hashes"]),
            verifier_id=str(data["verifier_id"]),
            created_at_utc=str(data["created_at_utc"]),
            dirty_workspace=bool(data.get("dirty_workspace", False)),
            uncommitted_snapshot_hash=data.get("uncommitted_snapshot_hash"),
        )


class EvidenceManifestRegistry:
    """Binds EvidenceManifest to ExecutionAuthority leases, AcceptanceSpec, and CAS EvidenceStore."""

    def __init__(
        self,
        *,
        authority: ExecutionAuthority,
        acceptance_registry: AcceptanceSpecRegistry,
        evidence_store: ContentAddressedEvidenceStore,
        clock: Clock | None = None,
    ) -> None:
        self.authority = authority
        self.store = authority.store
        self.acceptance_registry = acceptance_registry
        self.evidence_store = evidence_store
        self.clock: Clock = clock or authority.clock
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_manifests (
                    evidence_id TEXT PRIMARY KEY,
                    task_unique_key TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    fencing_epoch INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE,
                    FOREIGN KEY (execution_id) REFERENCES executions(execution_id) ON DELETE CASCADE
                );
                """
            )

    def create_and_persist_manifest(
        self,
        *,
        task_spec: TaskSpec,
        execution_id: str,
        worker_id: str,
        fencing_epoch: int,
        workspace_id: str,
        git_state: TrustedGitState,
        environment: ExecutionEnvironmentRecord,
        artifact_hashes: dict[str, str],
        verifier_id: str,
        actor_role: str = "verifier",
    ) -> EvidenceManifest:
        if git_state.base_sha != task_spec.base_sha:
            raise VerificationFailedError("Manifest base SHA differs from TaskSpec.")
        trusted_spec, spec_hash = self._load_spec_unchecked_policy(task_spec, verifier_id)

        self.acceptance_registry.assert_verifier_authority(
            actor_role=actor_role,
            verifier_id=verifier_id,
            trusted_spec=trusted_spec,
        )

        # Store environment manifest in S28 store first (if storage fails, no DB record is written!)
        env_bytes = _canonical_json(environment.to_dict()).encode("utf-8")
        env_rec = self.evidence_store.store_bytes(
            env_bytes,
            kind=EvidenceKind.ENVIRONMENT_MANIFEST,
            actor_role=actor_role,
        )
        env_hash = environment.environment_hash()

        all_artifacts = dict(artifact_hashes)
        all_artifacts["environment_manifest"] = env_rec.digest
        if git_state.uncommitted_snapshot_hash is not None:
            all_artifacts["uncommitted_snapshot"] = git_state.uncommitted_snapshot_hash

        # Re-verify every referenced artifact exists and matches its SHA-256
        for name, digest in all_artifacts.items():
            if not self.evidence_store.verify_digest(digest):
                raise VerificationFailedError(
                    f"Artifact '{name}' ({digest}) missing from content-addressed evidence store."
                )

        manifest = EvidenceManifest(
            schema_version=EVIDENCE_MANIFEST_SCHEMA_VERSION,
            evidence_id=str(uuid.uuid4()),
            project_id=task_spec.project_id,
            repo_id=task_spec.repo_id,
            task_unique_key=task_spec.unique_key,
            execution_id=execution_id,
            worker_id=worker_id,
            fencing_epoch=fencing_epoch,
            workspace_id=workspace_id,
            base_sha=git_state.base_sha,
            tested_commit_sha=git_state.tested_commit_sha,
            tested_tree_sha=git_state.tested_tree_sha,
            dirty_workspace=git_state.dirty_workspace,
            uncommitted_snapshot_hash=git_state.uncommitted_snapshot_hash,
            acceptance_hash=spec_hash,
            policy_hash=trusted_spec.policy_hash,
            environment_hash=env_hash,
            artifact_hashes=all_artifacts,
            verifier_id=verifier_id,
            created_at_utc=self.clock.now_iso(),
        )
        manifest_hash = manifest.manifest_hash()
        manifest_json = _canonical_json(manifest.to_dict())

        # Validate active lease & fencing_epoch atomically inside the persistence transaction
        now_dt = self.clock.now_utc()
        with self.store.transaction() as conn:
            lease = conn.execute(
                "SELECT execution_id, worker_id, fencing_epoch, expires_at_utc FROM leases WHERE task_unique_key = ?;",
                (task_spec.unique_key,),
            ).fetchone()
            if lease is None:
                raise LeaseExpiredError(f"No active lease for task '{task_spec.unique_key}'.")
            if int(lease["fencing_epoch"]) != fencing_epoch:
                raise StaleEpochError(
                    f"Fencing epoch mismatch: expected {lease['fencing_epoch']}, got {fencing_epoch}."
                )
            if str(lease["execution_id"]) != execution_id or str(lease["worker_id"]) != worker_id:
                raise StaleEpochError("Execution ID or worker ID does not match active lease.")
            if _parse_utc(str(lease["expires_at_utc"])) <= now_dt:
                raise LeaseExpiredError(f"Lease expired for task '{task_spec.unique_key}'.")

            conn.execute(
                """
                INSERT INTO evidence_manifests (
                    evidence_id, task_unique_key, execution_id, fencing_epoch,
                    manifest_hash, manifest_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    manifest.evidence_id,
                    manifest.task_unique_key,
                    manifest.execution_id,
                    manifest.fencing_epoch,
                    manifest_hash,
                    manifest_json,
                    manifest.created_at_utc,
                ),
            )
        return manifest

    def _load_spec_unchecked_policy(self, task_spec: TaskSpec, verifier_id: str):
        with self.store.open_connection() as conn:
            row = conn.execute(
                "SELECT policy_hash FROM acceptance_specs WHERE task_unique_key = ?;",
                (task_spec.unique_key,),
            ).fetchone()
        if row is None:
            raise VerificationFailedError(f"Missing AcceptanceSpec for '{task_spec.unique_key}'.")
        return self.acceptance_registry.load_and_verify_spec(
            task_spec=task_spec,
            expected_policy_hash=str(row["policy_hash"]),
            verifier_id=verifier_id,
        )

    def verify_manifest(
        self,
        evidence_id: str,
        *,
        task_spec: TaskSpec,
        expected_execution_id: str,
        expected_fencing_epoch: int,
        expected_policy_hash: str,
        expected_git_state: TrustedGitState,
        expected_environment: ExecutionEnvironmentRecord,
    ) -> EvidenceManifest:
        """Re-verify persisted manifest against current spec, git state, environment, and CAS blobs."""
        with self.store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM evidence_manifests WHERE evidence_id = ?;",
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise VerificationFailedError(f"EvidenceManifest '{evidence_id}' not found.")

        manifest = EvidenceManifest.from_dict(json.loads(str(row["manifest_json"])))
        if manifest.manifest_hash() != str(row["manifest_hash"]):
            raise VerificationFailedError("EvidenceManifest DB hash mismatch (tampered).")

        if (manifest.task_unique_key != task_spec.unique_key or manifest.project_id != task_spec.project_id
                or manifest.repo_id != task_spec.repo_id or manifest.base_sha != task_spec.base_sha
                or expected_git_state.base_sha != task_spec.base_sha):
            raise VerificationFailedError("EvidenceManifest cross-task or cross-project replay rejected.")
        if manifest.execution_id != expected_execution_id or manifest.fencing_epoch != expected_fencing_epoch:
            raise VerificationFailedError("EvidenceManifest cross-execution or stale-epoch replay rejected.")

        _, spec_hash = self.acceptance_registry.load_and_verify_spec(
            task_spec=task_spec,
            expected_policy_hash=expected_policy_hash,
            expected_spec_hash=manifest.acceptance_hash,
            verifier_id=manifest.verifier_id,
        )
        if manifest.acceptance_hash != spec_hash or manifest.policy_hash != expected_policy_hash:
            raise VerificationFailedError("EvidenceManifest acceptance/policy hash mismatch.")

        if (
            manifest.tested_commit_sha != expected_git_state.tested_commit_sha
            or manifest.tested_tree_sha != expected_git_state.tested_tree_sha
            or manifest.dirty_workspace != expected_git_state.dirty_workspace
            or manifest.uncommitted_snapshot_hash != expected_git_state.uncommitted_snapshot_hash
        ):
            raise VerificationFailedError("EvidenceManifest Git SHA or uncommitted snapshot mismatch.")

        if manifest.environment_hash != expected_environment.environment_hash():
            raise VerificationFailedError("EvidenceManifest execution environment mismatch.")

        for name, digest in manifest.artifact_hashes.items():
            if not self.evidence_store.verify_digest(digest):
                raise VerificationFailedError(f"Artifact '{name}' ({digest}) missing or invalid.")

        return manifest
