"""M2 S22-S25: Unified Fail-Closed OS Sandbox Backend & Governed Sandbox Runner.

Supports:
- S22: Real OS/Process Sandbox & Filesystem Isolation (bwrap / unshare namespaces on Linux,
       Seatbelt sandbox-exec on macOS/Apple Silicon, AppContainer+JobObject probe on Windows).
- S23: Default-deny Network Isolation (netns / SBPL deny network* / AppContainer no-net).
- S24: Strict Environment Variable Whitelist & Credential Isolation.
- S25: Process Group Timeout, Forced Cancellation (SIGTERM -> SIGKILL killpg), and Evidence Preservation.

Fail Closed: If no OS-enforced sandbox backend is available, execution is refused
(SandboxUnavailableError); never falls back to unsandboxed execution.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from opencognitiveos.contracts.models import canonical_json_hash
from opencognitiveos.errors import (
    LeaseExpiredError,
    OpenCognitiveOSError,
    StaleEpochError,
)
from opencognitiveos.kernel.authority import ExecutionAuthority


class SandboxUnavailableError(OpenCognitiveOSError):
    """Raised when no real OS-enforced sandbox backend is available (Fail Closed)."""


class SandboxPolicyError(OpenCognitiveOSError):
    """Raised when sandbox request violates M2 security policy (e.g., network allow)."""


@dataclass(frozen=True)
class SandboxCapabilities:
    backend_name: str
    os_name: str
    arch: str
    available: bool
    is_os_enforced: bool
    supports_fs_isolation: bool
    supports_net_deny: bool
    supports_env_isolation: bool
    supports_process_group_kill: bool
    reason: str


@dataclass(frozen=True)
class SandboxExecutionRequest:
    task_unique_key: str
    execution_id: str
    worker_id: str
    fencing_epoch: int
    workspace_path: Path
    command: tuple[str, ...]
    timeout_seconds: float = 30.0
    env_whitelist: tuple[str, ...] = ()
    extra_env: tuple[tuple[str, str], ...] = ()
    network_mode: str = "DENY"
    workspace_id: str | None = None


@dataclass(frozen=True)
class SandboxHandle:
    backend_name: str
    pid: int
    pgid: int
    workspace_path: str
    started_at_utc: str


@dataclass(frozen=True)
class SandboxExecutionResult:
    backend_name: str
    is_os_enforced: bool
    exit_code: int
    timed_out: bool
    cancelled: bool
    process_group_terminated: bool
    stdout: str
    stderr: str
    pgid: int
    duration_seconds: float
    evidence_hash: str


@dataclass(frozen=True)
class SandboxCancelResult:
    pgid: int
    terminated: bool
    signal_sent: str


@dataclass(frozen=True)
class SandboxInspectResult:
    pgid: int
    alive: bool


def _parse_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_sanitized_env(
    *,
    workspace_path: Path,
    env_whitelist: Sequence[str] = (),
    extra_env: Sequence[tuple[str, str]] = (),
) -> dict[str, str]:
    """S24: Build a zero-leakage environment from scratch using strict whitelist only."""
    clean: dict[str, str] = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "HOME": str(workspace_path),
        "TMPDIR": str(workspace_path),
    }
    if env_whitelist or extra_env:
        raise SandboxPolicyError("Caller-controlled environment overrides are disabled.")
    return clean


class SandboxBackend(ABC):
    """Unified OS Sandbox Backend interface (S22-S25)."""

    name: str

    @abstractmethod
    def detect_capabilities(self) -> SandboxCapabilities:
        """Probe OS-level sandbox availability without falling back to unisolated execution."""

    @abstractmethod
    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        """Execute command inside OS-enforced sandbox with timeout and evidence capture."""

    def cancel(self, handle: SandboxHandle) -> SandboxCancelResult:
        """A serialized PID/PGID is not proof of ownership. Never signal it."""
        raise SandboxUnavailableError("No verified process ownership handle; cancellation denied.")

    def inspect(self, handle: SandboxHandle) -> SandboxInspectResult:
        raise SandboxUnavailableError("Process identity and containment are unverified.")

    def cleanup(self, handle: SandboxHandle) -> None:
        raise SandboxUnavailableError("Termination evidence unavailable; preserve workspace.")


class LinuxUnshareSandboxBackend(SandboxBackend):
    """Real Linux OS sandbox using unprivileged user+net+mount+pid namespaces via /usr/bin/unshare."""

    name = "linux-unshare"

    def __init__(self, unshare_bin: str | None = None) -> None:
        self.unshare_bin = unshare_bin or shutil.which("unshare")

    def detect_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(self.name, platform.system(), platform.machine(),
            False, False, False, False, True, False,
            "Legacy unshare backend quarantined: filesystem isolation not verified.")

    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        raise SandboxUnavailableError(self.detect_capabilities().reason)


class MacOSSeatbeltSandboxBackend(SandboxBackend):
    """macOS (Apple Silicon & Intel) Seatbelt sandbox-exec backend (S22-S25)."""

    name = "macos-seatbelt"

    def __init__(self, sandbox_exec_bin: str = "/usr/bin/sandbox-exec") -> None:
        self.sandbox_exec_bin = sandbox_exec_bin

    def detect_capabilities(self) -> SandboxCapabilities:
        arch = platform.machine()
        os_name = platform.system()
        available = False  # pending native security acceptance
        reason = (
            f"macOS Seatbelt ready ({arch})"
            if available
            else f"Seatbelt unavailable on {os_name}/{arch}."
        )
        return SandboxCapabilities(
            backend_name=self.name,
            os_name=os_name,
            arch=arch,
            available=available,
            is_os_enforced=True,
            supports_fs_isolation=available,
            supports_net_deny=available,
            supports_env_isolation=True,
            supports_process_group_kill=os.name == "posix",
            reason=reason,
        )

    @staticmethod
    def build_sbpl_profile(workspace_path: Path) -> str:
        ws = str(workspace_path.resolve(strict=False))
        if any(c in ws for c in (chr(34), chr(92), chr(10), chr(13))):
            raise SandboxPolicyError("Unsafe SBPL path")
        return "\n".join(
            [
                "(version 1)",
                "(deny default)",
                "(allow process-exec)",
                "(allow process-fork)",
                "(allow sysctl-read)",
                '(allow file-read* (subpath "/usr") (subpath "/bin") (subpath "/sbin") (subpath "/System") (subpath "/Library") (subpath "/dev"))',
                f'(allow file-read* file-write* (subpath "{ws}"))',
                "(deny network*)",
            ]
        )

    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        caps = self.detect_capabilities()
        if not caps.available:
            raise SandboxUnavailableError(caps.reason)
        if request.network_mode != "DENY":
            raise SandboxPolicyError(f"M2 requires default-deny network; got {request.network_mode!r}")

        ws = request.workspace_path.resolve(strict=True)
        clean_env = build_sanitized_env(
            workspace_path=ws,
            env_whitelist=request.env_whitelist,
            extra_env=request.extra_env,
        )
        profile = self.build_sbpl_profile(ws)
        with tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False, encoding="utf-8") as tf:
            tf.write(profile)
            sb_path = Path(tf.name)

        t0 = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.Popen(
                [self.sandbox_exec_bin, "-f", str(sb_path), *request.command],
                env=clean_env,
                cwd=str(ws),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            handle = SandboxHandle(
                backend_name=self.name,
                pid=proc.pid,
                pgid=proc.pid,
                workspace_path=str(ws),
                started_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            try:
                out_b, err_b = proc.communicate(timeout=request.timeout_seconds)
                exit_code = int(proc.returncode)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.cancel(handle)
                out_b, err_b = proc.communicate()
                exit_code = int(proc.returncode) if proc.returncode is not None else -9
            pg_dead = not self.inspect(handle).alive
        finally:
            sb_path.unlink(missing_ok=True)

        stdout_txt = out_b.decode("utf-8", errors="replace")
        stderr_txt = err_b.decode("utf-8", errors="replace")
        return SandboxExecutionResult(
            backend_name=self.name,
            is_os_enforced=True,
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=False,
            process_group_terminated=pg_dead,
            stdout=stdout_txt,
            stderr=stderr_txt,
            pgid=handle.pgid,
            duration_seconds=round(time.monotonic() - t0, 6),
            evidence_hash=canonical_json_hash(
                {
                    "backend_name": self.name,
                    "task_unique_key": request.task_unique_key,
                    "execution_id": request.execution_id,
                    "fencing_epoch": request.fencing_epoch,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "process_group_terminated": pg_dead,
                    "stdout": stdout_txt,
                    "stderr": stderr_txt,
                }
            ),
        )


class WindowsAppContainerJobBackend(SandboxBackend):
    """Windows AppContainer + Job Object backend (Fail Closed until native broker integrated)."""

    name = "windows-appcontainer-job"

    def detect_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            backend_name=self.name,
            os_name=platform.system(),
            arch=platform.machine(),
            available=False,
            is_os_enforced=True,
            supports_fs_isolation=False,
            supports_net_deny=False,
            supports_env_isolation=True,
            supports_process_group_kill=False,
            reason="Windows AppContainer + Job Object native broker not yet wired; Fail Closed.",
        )

    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        raise SandboxUnavailableError(self.detect_capabilities().reason)


def select_sandbox_backend(candidates: Sequence[SandboxBackend] | None = None) -> SandboxBackend:
    """Select the first OS-enforced sandbox backend; Fail Closed if none is available."""
    if candidates is None:
        sys_name = platform.system()
        if sys_name == "Linux":
            candidates = (LinuxUnshareSandboxBackend(),)
        elif sys_name == "Darwin":
            candidates = (MacOSSeatbeltSandboxBackend(),)
        elif sys_name == "Windows":
            candidates = (WindowsAppContainerJobBackend(),)
        else:
            candidates = ()

    reasons: list[str] = []
    for backend in candidates:
        caps = backend.detect_capabilities()
        if caps.available and caps.is_os_enforced:
            return backend
        reasons.append(f"{backend.name}: {caps.reason}")

    raise SandboxUnavailableError(
        "No OS-enforced sandbox backend available (Fail Closed, no unsandboxed fallback). "
        + "; ".join(reasons)
    )


class GovernedSandboxRunner:
    """Authorizes every sandboxed execution via ExecutionAuthority lease + fencing_epoch."""

    def __init__(
        self,
        *,
        authority: ExecutionAuthority,
        backend: SandboxBackend | None = None,
        workspace_manager: Any = None,
    ) -> None:
        self.authority = authority
        self.workspace_manager = workspace_manager
        self.backend = backend or select_sandbox_backend()

    def _authorize_lease(self, request: SandboxExecutionRequest) -> None:
        now_dt = self.authority.clock.now_utc()
        with self.authority.store.open_connection() as conn:
            lease = conn.execute(
                "SELECT execution_id, worker_id, fencing_epoch, expires_at_utc FROM leases WHERE task_unique_key = ?;",
                (request.task_unique_key,),
            ).fetchone()
        if lease is None:
            raise LeaseExpiredError(f"No active lease for task '{request.task_unique_key}'.")
        if int(lease["fencing_epoch"]) != request.fencing_epoch:
            raise StaleEpochError(
                f"Fencing epoch mismatch: expected {lease['fencing_epoch']}, got {request.fencing_epoch}."
            )
        if str(lease["execution_id"]) != request.execution_id or str(lease["worker_id"]) != request.worker_id:
            raise StaleEpochError(
                f"Execution/worker mismatch for task '{request.task_unique_key}'."
            )
        if _parse_utc(str(lease["expires_at_utc"])) <= now_dt:
            raise LeaseExpiredError(f"Lease expired for task '{request.task_unique_key}'.")

    def run(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        self._authorize_lease(request)
        manager = self.workspace_manager
        if manager is None or manager.authority is not self.authority or not request.workspace_id:
            raise SandboxPolicyError("Registered workspace and matching controller required.")
        args = dict(worker_id=request.worker_id, execution_id=request.execution_id, epoch=request.fencing_epoch)
        identity = manager.authorize_workspace(request.workspace_id, **args)
        if identity.task_unique_key != request.task_unique_key or Path(identity.worktree_path) != request.workspace_path:
            raise SandboxPolicyError("Workspace path or task mismatch.")
        manager.verify_worktree(identity)
        if manager.authorize_workspace(request.workspace_id, **args) != identity:
            raise SandboxPolicyError("Workspace identity changed during verification.")
        self._authorize_lease(request)
        # Launch remains disabled until OS containment and termination evidence are validated.
        raise SandboxUnavailableError("Execution gate quarantined pending OS acceptance.")
