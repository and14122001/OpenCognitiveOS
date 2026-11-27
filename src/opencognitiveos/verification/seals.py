"""Domain-separated controller attestations. Keys stay outside SQLite and worker mounts."""
import hmac


def seal(key: bytes, kind: str, digest: str) -> str:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("A controller-managed key of at least 32 bytes is required.")
    return hmac.digest(key, f"ocos-v1/{kind}/{digest}".encode("ascii"), "sha256").hex()
