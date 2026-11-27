"""S28: Content-Addressed Evidence Store (evidence/sha256/<prefix>/<digest>).

Security & Atomicity Boundary Notice:
- Filesystem content-addressed storage and SQLite metadata do NOT form a cross-system
  distributed atomic transaction.
- Staging orphans from interrupted writes are cleaned via recover_orphaned_staging();
  any DB commit failure leaves content-addressed blobs immutable and never creates a
  successful VerificationReceipt.
- SHA-256 integrity verifies content matches its digest, NOT origin authenticity,
  and cannot defend against an attacker with full controller and DB write access.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sqlite3
from contextlib import closing, contextmanager
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from opencognitiveos.contracts.models import Clock, SystemClock
from opencognitiveos.errors import PolicyDeniedError, VerificationFailedError

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_SIZE = 65536

_SECRET_PATTERNS = (
    re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
    re.compile(rb"ghp_[0-9A-Za-z]{36}"),
    re.compile(rb"github_pat_[0-9A-Za-z_]{20,}"),
    re.compile(rb"sk-[0-9A-Za-z]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?:OPENAI_API_KEY|GITHUB_TOKEN|GEMINI_API_KEY|AWS_SECRET_ACCESS_KEY)\s*=\s*\S+"),
)


class EvidenceKind(str, Enum):
    RAW_EVIDENCE = "RAW_EVIDENCE"
    ACCEPTANCE_REPORT = "ACCEPTANCE_REPORT"
    EXECUTION_LOG = "EXECUTION_LOG"
    ENVIRONMENT_MANIFEST = "ENVIRONMENT_MANIFEST"


class EvidenceIntegrityError(VerificationFailedError):
    """Raised when stored evidence fails SHA-256 verification, path safety, or capacity checks."""


@dataclass(frozen=True)
class StoredEvidenceRecord:
    digest: str
    kind: EvidenceKind
    size_bytes: int
    relative_path: str
    deduplicated: bool
    created_at_utc: str


def _contains_secret(data: bytes) -> bool:
    return any(pat.search(data) is not None for pat in _SECRET_PATTERNS)


def _assert_safe_node(path: Path) -> None:
    """Reject symlinks, hardlinks (st_nlink > 1 on files), and special files (FIFO/socket/device)."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise EvidenceIntegrityError(f"Symlink forbidden in evidence store: '{path}'.")
    if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
        raise EvidenceIntegrityError(f"Special file forbidden in evidence store: '{path}'.")
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise EvidenceIntegrityError(f"Hardlink (st_nlink={st.st_nlink}) forbidden in evidence store: '{path}'.")
    win_attrs = getattr(st, "st_file_attributes", 0)
    if win_attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400):
        raise EvidenceIntegrityError(f"Reparse point forbidden in evidence store: '{path}'.")


class ContentAddressedEvidenceStore:
    """Immutable SHA-256 content-addressed store under <root>/evidence/sha256/<prefix>/<digest>."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        max_object_bytes: int = 16 * 1024 * 1024,
        max_total_bytes: int = 256 * 1024 * 1024,
        clock: Clock | None = None,
    ) -> None:
        if max_object_bytes <= 0 or max_total_bytes < max_object_bytes:
            raise ValueError("Invalid evidence store capacity limits.")
        supplied = Path(root_dir)
        if supplied.exists() or supplied.is_symlink():
            _assert_safe_node(supplied)
        self.root_dir = supplied.resolve(strict=False)
        self.root_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root_dir = self.root_dir.resolve(strict=True)
        self.evidence_root = self.root_dir / "evidence" / "sha256"
        self.staging_dir = self.root_dir / "evidence" / ".staging"
        for directory in (self.root_dir / "evidence", self.evidence_root, self.staging_dir):
            directory.mkdir(mode=0o700, exist_ok=True)
            _assert_safe_node(directory)
            if not directory.is_dir():
                raise EvidenceIntegrityError("Evidence hierarchy requires a directory.")
        self.max_object_bytes = int(max_object_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.clock: Clock = clock or SystemClock()
        self._lock_path = self.root_dir / "evidence" / ".writer.sqlite3"

    @contextmanager
    def _exclusive_writer(self):
        # One writer across store instances and processes, including crash recovery.
        self._validate_staging()
        if self._lock_path.exists() or self._lock_path.is_symlink():
            _assert_safe_node(self._lock_path)
        with closing(sqlite3.connect(self._lock_path, timeout=30)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            yield

    @staticmethod
    def validate_digest(digest: str) -> str:
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise EvidenceIntegrityError(f"Invalid SHA-256 digest: {digest!r}")
        return digest

    def _safe_object_path(self, digest: str) -> Path:
        self.validate_digest(digest)
        prefix = digest[:2]
        prefix_dir = self.evidence_root / prefix
        target = prefix_dir / digest
        # Verify no symlinks/reparse points along existing hierarchy
        for ancestor in (self.root_dir, self.root_dir / "evidence", self.evidence_root, prefix_dir, target):
            if ancestor.exists() or ancestor.is_symlink():
                _assert_safe_node(ancestor)
        if not target.resolve(strict=False).is_relative_to(self.evidence_root):
            raise EvidenceIntegrityError(f"Path traversal detected for digest '{digest}'.")
        return target

    def current_total_bytes(self) -> int:
        total = 0
        for p in self.evidence_root.rglob("*"):
            if p.is_symlink():
                raise EvidenceIntegrityError(f"Symlink detected during capacity scan: '{p}'.")
            if p.is_file():
                _assert_safe_node(p)
                total += p.stat().st_size
        return total

    def store_bytes(
        self,
        data: bytes,
        *,
        kind: EvidenceKind,
        actor_role: str = "controller",
    ) -> StoredEvidenceRecord:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Evidence data must be bytes.")
        raw = bytes(data)
        return self.store_stream(
            _BytesStream(raw),
            kind=kind,
            actor_role=actor_role,
        )

    def store_stream(
        self,
        stream: BinaryIO,
        *,
        kind: EvidenceKind,
        actor_role: str = "controller",
    ) -> StoredEvidenceRecord:
        """Stream evidence into staging with size/secret checks, then atomically commit by SHA-256."""
        if actor_role == "worker":
            raise PolicyDeniedError("Worker cannot directly write or overwrite immutable evidence store.")
        if actor_role not in {"controller", "verifier"}:
            raise PolicyDeniedError(f"Unauthorized actor_role '{actor_role}' for evidence write.")

        with self._exclusive_writer():
            return self._write_locked(stream, kind)

    def _write_locked(self, stream: BinaryIO, kind: EvidenceKind) -> StoredEvidenceRecord:
        hasher = hashlib.sha256()
        total_len = 0
        tail_overlap = b""
        tmp_name = f".tmp-{uuid.uuid4().hex}"
        tmp_path = self.staging_dir / tmp_name

        self._validate_staging()
        fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total_len += len(chunk)
                if total_len > self.max_object_bytes:
                    raise EvidenceIntegrityError(
                        f"Evidence object exceeds max_object_bytes ({self.max_object_bytes})."
                    )
                window = tail_overlap + chunk
                if _contains_secret(window):
                    raise PolicyDeniedError("Secret/credential material forbidden in evidence store.")
                tail_overlap = window[-128:]
                hasher.update(chunk)
                view = memoryview(chunk)
                written = 0
                while written < len(chunk):
                    written += os.write(fd, view[written:])
            os.fsync(fd)
        except Exception:
            os.close(fd)
            tmp_path.unlink(missing_ok=True)
            raise
        os.close(fd)

        digest = hasher.hexdigest()
        rel_path = f"evidence/sha256/{digest[:2]}/{digest}"

        target = self._safe_object_path(digest)
        if target.exists():
            _assert_safe_node(target)
            # Re-verify existing content before deduplicating
            try:
                self.read_bytes(digest)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            tmp_path.unlink(missing_ok=True)
            return StoredEvidenceRecord(
                digest=digest,
                kind=kind,
                size_bytes=total_len,
                relative_path=rel_path,
                deduplicated=True,
                created_at_utc=self.clock.now_iso(),
            )

        if self.current_total_bytes() + total_len > self.max_total_bytes:
            tmp_path.unlink(missing_ok=True)
            raise EvidenceIntegrityError(
                f"Evidence store total capacity ({self.max_total_bytes} bytes) exceeded."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_node(target.parent)
        os.replace(tmp_path, target)
        if os.name == "posix":
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

        return StoredEvidenceRecord(
            digest=digest,
            kind=kind,
            size_bytes=total_len,
            relative_path=rel_path,
            deduplicated=False,
            created_at_utc=self.clock.now_iso(),
        )

    def read_bytes(self, digest: str) -> bytes:
        target = self._safe_object_path(digest)
        if not target.is_file():
            raise EvidenceIntegrityError(f"Evidence digest '{digest}' not found.")
        before = target.lstat()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(target, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not os.path.samestat(before, opened) or not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1 or opened.st_size > self.max_object_bytes):
                raise EvidenceIntegrityError("Unsafe or oversized evidence file.")
            data = stream.read(self.max_object_bytes + 1)
        if len(data) > self.max_object_bytes or hashlib.sha256(data).hexdigest() != digest:
            raise EvidenceIntegrityError(f"Evidence hash mismatch for '{digest}'.")
        return data

    def verify_digest(self, digest: str) -> bool:
        if not self._safe_object_path(digest).is_file():
            return False
        self.read_bytes(digest)
        return True

    def _validate_staging(self) -> None:
        for node in (self.root_dir, self.root_dir / "evidence", self.staging_dir):
            _assert_safe_node(node)
            if not node.is_dir():
                raise EvidenceIntegrityError("Evidence staging ancestor is not a directory.")

    def recover_orphaned_staging(self) -> tuple[str, ...]:
        """Remove incomplete staging files after crash/interruption without touching committed blobs."""
        self._validate_staging()
        cleaned: list[str] = []
        with self._exclusive_writer():
            for item in sorted(self.staging_dir.iterdir()):
                if item.name.startswith(".tmp-"):
                    item.unlink(missing_ok=True)
                    cleaned.append(item.name)
        return tuple(cleaned)


class _BytesStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        if size < 0:
            out = self._data[self._pos :]
            self._pos = len(self._data)
            return out
        out = self._data[self._pos : self._pos + size]
        self._pos += len(out)
        return out
