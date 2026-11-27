"""M2 S21-S22: Fixed-SHA Git Worktree & SQLite-Governed Workspace Lifecycle Manager.

Security Notice:
- Per Git official documentation, a git worktree still shares Git objects, config,
  and refs with the main repository; a worktree is NOT an OS security sandbox.
- Until OS-level sandbox isolation (S21-S30 remainder) is completed, this module
  does NOT claim complete security isolation and must never run untrusted code
  without OS sandboxing.
"""

from __future__ import annotations

import os
import json
import unicodedata
import re
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.errors import OpenCognitiveOSError
from opencognitiveos.execution.path_guard import checked_path
from opencognitiveos.kernel.authority import ExecutionAuthority

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _parse_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class WorkspaceConflictError(OpenCognitiveOSError):
    """Raised when a workspace ID or canonical target path is already claimed."""


class WorkspaceIdentityError(OpenCognitiveOSError):
    """Raised when workspace identity is missing, tampered, or mismatched."""


class WorktreeVerificationError(OpenCognitiveOSError):
    """Raised when git worktree verification or controlled git execution fails (Fail Closed)."""


@dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_id: str
    repo_id: str
    task_id: str
    task_unique_key: str
    repo_path: str
    base_sha: str
    worktree_path: str
    git_admin_dir: str | None
    lock_reason: str
    status: str  # CREATING | ACTIVE | NEEDS_RECONCILIATION | CLEANED
    created_at_utc: str
    updated_at_utc: str
    identity_hash: str

    @classmethod
    def compute_hash(
        cls,
        *,
        workspace_id: str,
        repo_id: str,
        task_id: str,
        task_unique_key: str,
        repo_path: str,
        base_sha: str,
        worktree_path: str,
        lock_reason: str,
    ) -> str:
        return canonical_json_hash(
            {
                "workspace_id": workspace_id,
                "repo_id": repo_id,
                "task_id": task_id,
                "task_unique_key": task_unique_key,
                "repo_path": repo_path,
                "base_sha": base_sha,
                "worktree_path": worktree_path,
                "lock_reason": lock_reason,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WorkspaceIdentity:
        return cls(
            workspace_id=str(row["workspace_id"]),
            repo_id=str(row["repo_id"]),
            task_id=str(row["task_id"]),
            task_unique_key=str(row["task_unique_key"]),
            repo_path=str(row["repo_path"]),
            base_sha=str(row["base_sha"]),
            worktree_path=str(row["canonical_worktree_path"]),
            git_admin_dir=str(row["git_admin_dir"]) if row["git_admin_dir"] is not None else None,
            lock_reason=str(row["lock_reason"]),
            status=str(row["status"]),
            created_at_utc=str(row["created_at_utc"]),
            updated_at_utc=str(row["updated_at_utc"]),
            identity_hash=str(row["identity_hash"]),
        )


@dataclass(frozen=True)
class WorkspaceRecoveryReport:
    cleaned_orphans: tuple[str, ...]
    skipped_active_lease: tuple[str, ...]
    skipped_unverified: tuple[str, ...]
    needs_reconciliation: tuple[str, ...]


class WorkspaceManager:
    """Manages fixed-SHA detached locked git worktrees under ExecutionAuthority SQLite governance."""

    def __init__(
        self,
        *,
        authority: ExecutionAuthority,
        repo_path: Path,
        workspaces_root: Path,
        git_executable: str | Path | None = None,
        git_timeout_seconds: float = 30.0,
    ) -> None:
        self.authority = authority
        self.store = authority.store
        self.clock = authority.clock

        if git_timeout_seconds <= 0:
            raise ValueError("git_timeout_seconds must be positive.")
        self.git_timeout_seconds = float(git_timeout_seconds)

        resolved_git = str(git_executable) if git_executable is not None else shutil.which("git")
        if not resolved_git:
            raise WorktreeVerificationError("Fixed git executable not found on PATH.")
        git_bin = Path(resolved_git)
        if not git_bin.is_absolute() or not git_bin.is_file() or not os.access(git_bin, os.X_OK):
            raise WorktreeVerificationError(f"Git executable must be an absolute executable file: {resolved_git!r}")
        self.git_executable = str(git_bin)

        self.repo_path = repo_path.resolve(strict=True)
        git_dir = self.repo_path / ".git"
        if not git_dir.is_dir():
            raise WorktreeVerificationError(f"Repository path '{self.repo_path}' does not contain a .git directory.")
        self.repo_git_dir = git_dir.resolve(strict=True)

        self.workspaces_root = workspaces_root.resolve(strict=False)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.workspaces_root = self.workspaces_root.resolve(strict=True)

        if self.workspaces_root.is_relative_to(self.repo_path) or self.repo_path.is_relative_to(self.workspaces_root):
            raise WorktreeVerificationError("Repository and workspace roots must be disjoint.")

        self._ensure_workspace_schema()

    def _ensure_workspace_schema(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_unique_key TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    canonical_worktree_path TEXT NOT NULL,
                    git_admin_dir TEXT,
                    lock_reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    identity_hash TEXT NOT NULL,
                    FOREIGN KEY (task_unique_key) REFERENCES tasks(unique_key) ON DELETE CASCADE
                );
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_active_path
                    ON workspaces(canonical_worktree_path) WHERE status != 'CLEANED';
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_workspaces_task
                    ON workspaces(task_unique_key, status);
                """
            )

            conn.execute("""CREATE TABLE IF NOT EXISTS native_private_workspaces (
                workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id), tag TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workspace_dispatch (
                workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
                execution_id TEXT NOT NULL UNIQUE REFERENCES executions(execution_id),
                epoch INTEGER NOT NULL, worker_id TEXT NOT NULL
            )""")

    def authorize_workspace(self, workspace_id: str, *, worker_id: str, execution_id: str, epoch: int) -> WorkspaceIdentity:
        """Controller precheck, not a process sandbox or a durable capability."""
        with self.store.transaction() as conn:
            row = conn.execute("SELECT w.*, t.fencing_epoch FROM workspaces w JOIN tasks t ON t.unique_key=w.task_unique_key WHERE w.workspace_id=?", (workspace_id,)).fetchone()
            binding = conn.execute("SELECT * FROM workspace_dispatch WHERE workspace_id=?", (workspace_id,)).fetchone()
            if row is None or row['status'] != 'ACTIVE' or binding is None or (binding['worker_id'], binding['execution_id'], binding['epoch']) != (worker_id, execution_id, epoch):
                raise WorkspaceIdentityError("Workspace dispatch mismatch.")
            ExecutionAuthority._validate_active_lease(conn, unique_key=row['task_unique_key'], current_epoch=row['fencing_epoch'], worker_id=worker_id, execution_id=execution_id, epoch=epoch, now_dt=self.clock.now_utc())
            ident = WorkspaceIdentity.from_row(row)
            self._verify_identity_hash(ident)
            return ident

    @staticmethod
    def _validate_id(value: str, label: str) -> str:
        if not isinstance(value, str) or not _SAFE_ID_RE.match(value) or ".." in value:
            raise ValueError(f"Invalid {label}: {value!r}")
        return value

    def _controlled_git_env(self) -> dict[str, str]:
        """Build a minimal, deterministic git environment rejecting external config/hook injection."""
        git_parent = str(Path(self.git_executable).parent)
        safe_path_parts = [git_parent]
        for std_dir in ("/usr/bin", "/bin"):
            if std_dir not in safe_path_parts and Path(std_dir).is_dir():
                safe_path_parts.append(std_dir)

        env: dict[str, str] = {
            "PATH": os.pathsep.join(safe_path_parts),
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
        }
        for win_key in ("SYSTEMROOT", "COMSPEC", "PATHEXT"):
            if win_key in os.environ:
                env[win_key] = os.environ[win_key]
        return env

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        cmd = [
            self.git_executable,
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "advice.detachedHead=false",
            "-C",
            str(self.repo_path),
            *args,
        ]
        try:
            return subprocess.run(
                cmd,
                env=self._controlled_git_env(),
                capture_output=True,
                check=check,
                timeout=self.git_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorktreeVerificationError(
                f"Git subprocess timed out after {self.git_timeout_seconds}s: {' '.join(args)}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            err = exc.stderr.decode("utf-8", errors="replace").strip() if exc.stderr else str(exc)
            raise WorktreeVerificationError(f"Git subprocess failed ({' '.join(args)}): {err}") from exc

    def validate_fixed_commit_sha(self, base_sha: str) -> str:
        """S21: Require full 40-hex commit SHA (reject branches, tags, HEAD, or short SHAs)."""
        if not isinstance(base_sha, str) or not _FULL_SHA_RE.match(base_sha):
            raise WorktreeVerificationError(
                f"Floating ref or non-40-hex SHA rejected (Fail Closed): {base_sha!r}"
            )
        res = self._run_git("rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}", check=False)
        if res.returncode != 0:
            raise WorktreeVerificationError(f"Commit SHA '{base_sha}' does not exist in repository.")
        resolved_sha = res.stdout.decode("utf-8", errors="strict").strip()
        if resolved_sha != base_sha:
            raise WorktreeVerificationError(
                f"Resolved commit SHA '{resolved_sha}' does not match requested '{base_sha}'."
            )
        return resolved_sha

    def list_git_worktrees(self) -> list[dict[str, Any]]:
        """Parse `git worktree list --porcelain -z` deterministically."""
        res = self._run_git("worktree", "list", "--porcelain", "-z", check=True)
        raw_tokens = res.stdout.split(b"\x00")
        entries: list[dict[str, Any]] = []
        current: dict[str, Any] = {}

        for raw in raw_tokens:
            token = raw.decode("utf-8", errors="strict")
            if token == "":
                if current:
                    entries.append(current)
                    current = {}
                continue
            if token.startswith("worktree "):
                current["worktree"] = token[len("worktree ") :]
            elif token.startswith("HEAD "):
                current["HEAD"] = token[len("HEAD ") :]
            elif token == "detached":
                current["detached"] = True
            elif token.startswith("branch "):
                current["branch"] = token[len("branch ") :]
            elif token == "locked":
                current["locked"] = True
                current["lock_reason"] = ""
            elif token.startswith("locked "):
                current["locked"] = True
                current["lock_reason"] = token[len("locked ") :]
            elif token == "prunable" or token.startswith("prunable "):
                current["prunable"] = True
            elif token == "bare":
                current["bare"] = True

        if current:
            entries.append(current)
        return entries

    @staticmethod
    def _verify_identity_hash(ident: WorkspaceIdentity) -> None:
        expected_hash = WorkspaceIdentity.compute_hash(
            workspace_id=ident.workspace_id,
            repo_id=ident.repo_id,
            task_id=ident.task_id,
            task_unique_key=ident.task_unique_key,
            repo_path=ident.repo_path,
            base_sha=ident.base_sha,
            worktree_path=ident.worktree_path,
            lock_reason=ident.lock_reason,
        )
        if ident.identity_hash != expected_hash:
            raise WorkspaceIdentityError(f"Workspace identity '{ident.workspace_id}' hash tampered or invalid.")

    def load_identity(self, workspace_id: str) -> WorkspaceIdentity:
        self._validate_id(workspace_id, "workspace_id")
        with self.store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?;",
                (workspace_id,),
            ).fetchone()
        if row is None:
            raise WorkspaceIdentityError(f"Workspace '{workspace_id}' not found in SQLite ExecutionAuthority store.")
        ident = WorkspaceIdentity.from_row(row)
        self._verify_identity_hash(ident)
        return ident

    def _mark_needs_reconciliation(self, workspace_id: str) -> None:
        now_iso = self.clock.now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE workspaces SET status = 'NEEDS_RECONCILIATION', updated_at_utc = ? WHERE workspace_id = ? AND status != 'CLEANED';",
                (now_iso, workspace_id),
            )

    def _verify_bidirectional_git_metadata(self, ident: WorkspaceIdentity, wt_path: Path) -> Path:
        """Verify .git pointer file and bidirectional binding with repo/.git/worktrees/<name>."""
        git_ptr = wt_path / ".git"
        if git_ptr.is_symlink() or not git_ptr.is_file():
            raise WorktreeVerificationError(f"Worktree '{wt_path}' missing regular .git pointer file.")

        try:
            ptr_text = git_ptr.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WorktreeVerificationError(f"Cannot read .git pointer file '{git_ptr}': {exc}") from exc

        if not ptr_text.startswith("gitdir: "):
            raise WorktreeVerificationError(f"Forged .git pointer content in '{git_ptr}': {ptr_text!r}")

        raw_admin = ptr_text[len("gitdir: ") :].strip()
        admin_candidate = Path(raw_admin)
        if not admin_candidate.is_absolute():
            admin_candidate = wt_path / admin_candidate
        try:
            admin_dir = admin_candidate.resolve(strict=True)
        except OSError as exc:
            raise WorktreeVerificationError(f"Git administrative directory does not exist: {raw_admin!r}") from exc

        expected_worktrees_dir = (self.repo_git_dir / "worktrees").resolve(strict=False)
        if not admin_dir.is_dir() or not admin_dir.is_relative_to(expected_worktrees_dir) or admin_dir == expected_worktrees_dir:
            raise WorktreeVerificationError(
                f"Git administrative directory '{admin_dir}' is outside repository worktrees dir '{expected_worktrees_dir}'."
            )

        if ident.git_admin_dir is not None and str(admin_dir) != ident.git_admin_dir:
            raise WorktreeVerificationError(
                f"Git administrative directory mismatch: expected '{ident.git_admin_dir}', got '{admin_dir}'."
            )

        # Bidirectional check 1: <admin_dir>/gitdir must point back to <wt_path>/.git
        back_gitdir = admin_dir / "gitdir"
        if back_gitdir.is_symlink() or not back_gitdir.is_file():
            raise WorktreeVerificationError(f"Missing administrative gitdir back-pointer in '{admin_dir}'.")
        back_raw = back_gitdir.read_text(encoding="utf-8").strip()
        back_path = Path(back_raw)
        if not back_path.is_absolute():
            back_path = admin_dir / back_path
        try:
            resolved_back = back_path.resolve(strict=True)
        except OSError as exc:
            raise WorktreeVerificationError(f"Administrative gitdir back-pointer broken: {back_raw!r}") from exc
        if resolved_back != git_ptr.resolve(strict=True):
            raise WorktreeVerificationError(
                f"Bidirectional gitdir mismatch: admin points to '{resolved_back}', expected '{git_ptr.resolve(strict=True)}'."
            )

        # Bidirectional check 2: <admin_dir>/commondir must resolve to self.repo_git_dir
        commondir_file = admin_dir / "commondir"
        if commondir_file.is_symlink() or not commondir_file.is_file():
            raise WorktreeVerificationError(f"Missing administrative commondir in '{admin_dir}'.")
        common_raw = commondir_file.read_text(encoding="utf-8").strip()
        common_path = Path(common_raw)
        if not common_path.is_absolute():
            common_path = admin_dir / common_path
        try:
            resolved_common = common_path.resolve(strict=True)
        except OSError as exc:
            raise WorktreeVerificationError(f"Administrative commondir broken: {common_raw!r}") from exc
        if resolved_common != self.repo_git_dir:
            raise WorktreeVerificationError(
                f"Repository commondir mismatch: expected '{self.repo_git_dir}', got '{resolved_common}'."
            )

        # Bidirectional check 3: <admin_dir>/locked must match exact lock_reason
        locked_file = admin_dir / "locked"
        if locked_file.is_symlink() or not locked_file.is_file():
            raise WorktreeVerificationError(f"Worktree administrative lock file missing in '{admin_dir}'.")
        locked_reason = locked_file.read_text(encoding="utf-8").strip()
        if locked_reason != ident.lock_reason:
            raise WorktreeVerificationError(
                f"Lock reason mismatch: expected {ident.lock_reason!r}, got {locked_reason!r}."
            )

        return admin_dir

    def verify_worktree(self, identity: WorkspaceIdentity) -> dict[str, Any]:
        """Verify repository, path ownership, bidirectional git metadata, detached SHA, and lock."""
        self._verify_identity_hash(identity)
        if Path(identity.repo_path).resolve(strict=False) != self.repo_path:
            raise WorktreeVerificationError(
                f"Repository path mismatch: identity bound to '{identity.repo_path}', manager is '{self.repo_path}'."
            )

        wt_path = Path(identity.worktree_path)
        if not wt_path.is_dir():
            raise WorktreeVerificationError(f"Worktree directory missing: '{wt_path}'.")

        rel = wt_path.relative_to(self.workspaces_root).as_posix()
        checked_path(self.workspaces_root, rel)

        self._verify_bidirectional_git_metadata(identity, wt_path)

        resolved_wt = wt_path.resolve(strict=True)
        for entry in self.list_git_worktrees():
            entry_path = Path(entry["worktree"]).resolve(strict=False)
            if entry_path == resolved_wt:
                if entry.get("HEAD") != identity.base_sha:
                    raise WorktreeVerificationError(
                        f"Worktree HEAD mismatch: expected {identity.base_sha}, got {entry.get('HEAD')}."
                    )
                if not entry.get("detached"):
                    raise WorktreeVerificationError(f"Worktree '{wt_path}' is not in detached HEAD mode.")
                if not entry.get("locked"):
                    raise WorktreeVerificationError(f"Worktree '{wt_path}' is not locked.")
                if entry.get("lock_reason") != identity.lock_reason:
                    raise WorktreeVerificationError(
                        f"Porcelain lock reason mismatch: expected {identity.lock_reason!r}, got {entry.get('lock_reason')!r}."
                    )
                return entry

        raise WorktreeVerificationError(f"Worktree '{wt_path}' not registered in `git worktree list --porcelain -z`.")

    def create_workspace(
        self,
        *,
        workspace_id: str,
        repo_id: str,
        task_id: str,
        base_sha: str,
        relative_dir: str | None = None,
        native_only: bool = False,
        controller_capability: object | None = None,
        before_finalize_hook: Callable[[Path], None] | None = None,
        before_commit_hook: Callable[[], None] | None = None,
    ) -> WorkspaceIdentity:
        """S21-S22: Atomically claim workspace_id + canonical path in SQLite and create locked worktree."""
        self._validate_id(workspace_id, "workspace_id")
        if not repo_id or not task_id:
            raise ValueError("repo_id and task_id must be non-empty.")
        verified_sha = self.validate_fixed_commit_sha(base_sha)

        rel = relative_dir if relative_dir is not None else workspace_id
        target_path = checked_path(self.workspaces_root, rel)
        canonical_target = str(target_path.resolve(strict=False))
        task_unique_key = f"{repo_id}::{task_id}"
        lock_reason = f"opencognitiveos:{workspace_id}:{task_id}"
        now_iso = self.clock.now_iso()

        ident_hash = WorkspaceIdentity.compute_hash(
            workspace_id=workspace_id,
            repo_id=repo_id,
            task_id=task_id,
            task_unique_key=task_unique_key,
            repo_path=str(self.repo_path),
            base_sha=verified_sha,
            worktree_path=canonical_target,
            lock_reason=lock_reason,
        )

        if target_path.exists():
            raise WorkspaceConflictError(f"Target workspace path '{target_path}' already exists on disk.")

        for entry in self.list_git_worktrees():
            if Path(entry["worktree"]).resolve(strict=False) == Path(canonical_target):
                raise WorkspaceConflictError(f"Path '{canonical_target}' is already a registered git worktree.")

        # Atomic SQLite claim (workspace_id PRIMARY KEY + canonical_worktree_path UNIQUE active index)
        with self.store.transaction() as conn:
            task_row = conn.execute(
                "SELECT unique_key, spec_json FROM tasks WHERE unique_key = ?;",
                (task_unique_key,),
            ).fetchone()
            if task_row is None:
                raise WorkspaceIdentityError(f"Task '{task_unique_key}' does not exist in ExecutionAuthority.")

            if native_only:
                gate = self.authority.evidence_completion
                if (gate is None or gate.manager is not self or controller_capability is not self.authority._controller_capability
                        or json.loads(task_row["spec_json"]).get("allowed_tools") != ["sandbox.python"]):
                    raise WorkspaceIdentityError("Private native worktrees require exclusive broker authority.")
            if json.loads(task_row["spec_json"]).get("base_sha") != verified_sha:
                raise WorkspaceIdentityError("Workspace SHA differs from TaskSpec.base_sha.")

            # Provision before dispatch; an existing lease may own a live worker.
            if conn.execute("SELECT 1 FROM leases WHERE task_unique_key = ?", (task_unique_key,)).fetchone():
                raise WorkspaceIdentityError("Cannot provision while a lease exists; reconcile first.")

            def path_key(value: str) -> str:
                return unicodedata.normalize("NFC", value).casefold()

            for claimed in conn.execute("SELECT canonical_worktree_path FROM workspaces WHERE status != 'CLEANED'"):
                existing = Path(path_key(claimed["canonical_worktree_path"]))
                proposed = Path(path_key(canonical_target))
                if existing.is_relative_to(proposed) or proposed.is_relative_to(existing):
                    raise WorkspaceConflictError("Portable workspace path already claimed.")

            try:
                conn.execute(
                    """
                    INSERT INTO workspaces (
                        workspace_id, repo_id, task_id, task_unique_key, repo_path,
                        base_sha, canonical_worktree_path, git_admin_dir, lock_reason,
                        status, created_at_utc, updated_at_utc, identity_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'CREATING', ?, ?, ?);
                    """,
                    (
                        workspace_id,
                        repo_id,
                        task_id,
                        task_unique_key,
                        str(self.repo_path),
                        verified_sha,
                        canonical_target,
                        lock_reason,
                        now_iso,
                        now_iso,
                        ident_hash,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkspaceConflictError(
                    f"Workspace '{workspace_id}' or canonical path '{canonical_target}' already claimed."
                ) from exc

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            res = self._run_git(
                "worktree",
                "add",
                "--detach",
                "--lock",
                "--reason",
                lock_reason,
                str(target_path),
                verified_sha,
                check=False,
            )
            if res.returncode != 0:
                err = res.stderr.decode("utf-8", errors="replace").strip()
                raise WorktreeVerificationError(f"git worktree add failed: {err}")
        except Exception:
            # External creation may have partially succeeded. Preserve evidence.
            self._mark_needs_reconciliation(workspace_id)
            raise

        if before_finalize_hook is not None:
            before_finalize_hook(target_path)

        resolved_wt = target_path.resolve(strict=True)
        provisional = WorkspaceIdentity(
            workspace_id=workspace_id,
            repo_id=repo_id,
            task_id=task_id,
            task_unique_key=task_unique_key,
            repo_path=str(self.repo_path),
            base_sha=verified_sha,
            worktree_path=str(resolved_wt),
            git_admin_dir=None,
            lock_reason=lock_reason,
            status="ACTIVE",
            created_at_utc=now_iso,
            updated_at_utc=now_iso,
            identity_hash=ident_hash,
        )
        self.verify_worktree(provisional)
        admin_dir = self._verify_bidirectional_git_metadata(provisional, resolved_wt)

        final_now = self.clock.now_iso()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT status, identity_hash FROM workspaces WHERE workspace_id = ?;",
                (workspace_id,),
            ).fetchone()
            if row is None or row["status"] != "CREATING" or row["identity_hash"] != ident_hash:
                raise WorkspaceIdentityError(f"Workspace '{workspace_id}' state changed before finalize.")

            conn.execute(
                """
                UPDATE workspaces
                SET status = 'ACTIVE', git_admin_dir = ?, updated_at_utc = ?
                WHERE workspace_id = ? AND status = 'CREATING';
                """,
                (str(admin_dir), final_now, workspace_id),
            )
            if native_only:
                from opencognitiveos.execution.native_cleanup import workspace_tag
                tag = workspace_tag(self.authority.evidence_completion.controller_key, ident_hash)
                conn.execute("INSERT INTO native_private_workspaces VALUES (?,?)", (workspace_id, tag))
            if before_commit_hook is not None:
                before_commit_hook()

        return self.load_identity(workspace_id)

    def cleanup_workspace(
        self,
        workspace_id: str,
        *,
        expected_repo_id: str,
        expected_task_id: str,
        expected_base_sha: str,
    ) -> WorkspaceIdentity:
        """S22: Verify repo, admin dir, lock reason, identity, and path ownership before removal."""
        ident = self.load_identity(workspace_id)

        # Cross-task / caller mismatch: refuse without touching valid workspace
        if (
            ident.repo_id != expected_repo_id
            or ident.task_id != expected_task_id
            or ident.base_sha != expected_base_sha
        ):
            raise WorkspaceIdentityError(
                f"Workspace '{workspace_id}' identity mismatch: refusal to delete unverified workspace."
            )

        if ident.status == "CLEANED":
            return ident
        if ident.status == "NEEDS_RECONCILIATION":
            raise WorktreeVerificationError(
                f"Workspace '{workspace_id}' is in NEEDS_RECONCILIATION and cannot be deleted automatically."
            )

        with self.store.transaction() as conn:
            if self._has_active_lease(conn, ident.task_unique_key, self.clock.now_utc()):
                raise WorkspaceIdentityError("Cannot clean a workspace with an active lease.")
            if conn.execute("SELECT 1 FROM leases WHERE task_unique_key = ?", (ident.task_unique_key,)).fetchone():
                raise WorkspaceIdentityError("Expired lease does not establish worker quiescence.")
            dispatch = conn.execute("SELECT * FROM workspace_dispatch WHERE workspace_id = ?", (workspace_id,)).fetchone()
            if dispatch:
                from opencognitiveos.execution.native_cleanup import ready
                if not ready(self, conn, ident, dispatch):
                    raise WorkspaceIdentityError("Dispatched workspace requires independent process termination evidence.")
            claimed = conn.execute("UPDATE workspaces SET status = 'CLEANING', updated_at_utc = ? WHERE workspace_id = ? AND status = 'ACTIVE' AND identity_hash = ?", (self.clock.now_iso(), workspace_id, ident.identity_hash))
            if claimed.rowcount != 1:
                raise WorkspaceIdentityError("Workspace is not available for cleanup.")
        wt_path = Path(ident.worktree_path)
        try:
            self.verify_worktree(ident)
        except Exception as exc:
            self._mark_needs_reconciliation(workspace_id)
            raise WorktreeVerificationError(
                f"Cleanup verification failed for '{workspace_id}'; transitioned to NEEDS_RECONCILIATION: {exc}"
            ) from exc

        try:
            unlock_res = self._run_git("worktree", "unlock", str(wt_path), check=False)
            if unlock_res.returncode != 0:
                self._mark_needs_reconciliation(workspace_id)
                err = unlock_res.stderr.decode("utf-8", errors="replace").strip()
                raise WorktreeVerificationError(
                    f"git worktree unlock failed for '{workspace_id}'; transitioned to NEEDS_RECONCILIATION: {err}"
                )

            rem_res = self._run_git("worktree", "remove", str(wt_path), check=False)
            if rem_res.returncode != 0 or wt_path.exists():
                self._mark_needs_reconciliation(workspace_id)
                err = rem_res.stderr.decode("utf-8", errors="replace").strip()
                raise WorktreeVerificationError(
                    f"git worktree remove failed for '{workspace_id}'; transitioned to NEEDS_RECONCILIATION: {err}"
                )

        except Exception:
            self._mark_needs_reconciliation(workspace_id)
            raise

        now_iso = self.clock.now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE workspaces SET status = 'CLEANED', updated_at_utc = ? WHERE workspace_id = ? AND status = 'CLEANING';",
                (now_iso, workspace_id),
            )
        return self.load_identity(workspace_id)

    def _has_active_lease(self, conn: sqlite3.Connection, task_unique_key: str, now_dt: datetime) -> bool:
        row = conn.execute(
            "SELECT expires_at_utc FROM leases WHERE task_unique_key = ?;",
            (task_unique_key,),
        ).fetchone()
        if row is None:
            return False
        return _parse_utc(str(row["expires_at_utc"])) > now_dt

    def recover_workspaces(self) -> WorkspaceRecoveryReport:
        """Inspect only. Interrupted operations require explicit reconciliation.

        Lease expiry is not process termination. Never unlock or remove here.
        No filesystem or Git operations run inside a database transaction.
        """
        skipped_active_lease = []
        skipped_unverified = []
        needs_reconciliation = []
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT * FROM workspaces WHERE status != 'CLEANED' ORDER BY created_at_utc, workspace_id").fetchall()
            for row in rows:
                ident = WorkspaceIdentity.from_row(row)
                if self._has_active_lease(conn, ident.task_unique_key, self.clock.now_utc()):
                    skipped_active_lease.append(ident.workspace_id)
                    continue
                invalid = False
                try:
                    self._verify_identity_hash(ident)
                except WorkspaceIdentityError:
                    invalid = True
                    skipped_unverified.append(ident.workspace_id)
                if invalid or ident.status in {"CREATING", "CLEANING", "NEEDS_RECONCILIATION"}:
                    needs_reconciliation.append(ident.workspace_id)
                    # Keep in-flight claims intact: a concurrent owner may still finalize.
                    continue
                if ident.status == "ACTIVE":
                    # Without proven quiescence, even verification can race a worker.
                    needs_reconciliation.append(ident.workspace_id)
        return WorkspaceRecoveryReport(
            cleaned_orphans=(),
            skipped_active_lease=tuple(skipped_active_lease),
            skipped_unverified=tuple(skipped_unverified),
            needs_reconciliation=tuple(needs_reconciliation),
        )
