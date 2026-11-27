"""Trusted controller precheck only; not an OS sandbox."""
from pathlib import Path
import stat

_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} |
                      {prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"})
_FORBIDDEN = frozenset('<>:"/\\|?*')

def _forbidden(part: str) -> bool:
    return (part in ("", ".", "..") or part.casefold() == ".git"
            or part.rstrip(" .") != part
            or any(ord(c) < 32 or c in _FORBIDDEN for c in part)
            or part.split(".", 1)[0].rstrip(" ").upper() in _RESERVED)

def checked_path(root: Path, relative: str) -> Path:
    """Caller must prevent concurrent mutation; never authorize untrusted processes."""
    if not isinstance(relative, str) or any(_forbidden(p) for p in relative.split("/")):
        raise ValueError("forbidden component")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("invalid root")
    current = root
    for part in relative.split("/"):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("link or reparse point forbidden")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("special file forbidden")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("hardlink forbidden")
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("outside root")
    return resolved
