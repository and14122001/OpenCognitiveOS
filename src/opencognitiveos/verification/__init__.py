"""Independent verification module (separate from Provider self-declaration)."""

from opencognitiveos.errors import VerificationFailedError
from opencognitiveos.verification.verifier import IndependentVerifier

__all__ = ["IndependentVerifier", "VerificationFailedError"]
