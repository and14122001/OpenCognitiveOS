"""M2 preliminary guard for trusted controller operations, not an OS sandbox."""
from pathlib import Path, PureWindowsPath

def checked_path(root: Path, relative: str) -> Path:
    """Reject traversal, links, special files and Git metadata.
    Caller must prevent concurrent mutation; never authorize untrusted processes.
    """
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise ValueError("invalid relative path")
    parts = relative.split("/")
    if any(p in ("", ".", "..") or p.casefold() == ".git" or ":" in p or p.rstrip(" .") != p or PureWindowsPath(p).is_reserved() for p in parts):
        raise ValueError("forbidden component")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("invalid root")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("link forbidden")
        if current.exists() and not (current.is_dir() or current.is_file()):
            raise ValueError("special file forbidden")
        if current.is_file() and current.stat().st_nlink != 1:
            raise ValueError("hardlink forbidden")
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("outside root")
    return resolved
