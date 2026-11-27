"""Independent verification, acceptance tamper protection (S26), CAS evidence (S28), and manifest binding (S29)."""

from opencognitiveos.verification.acceptance import (
    ACCEPTANCE_SPEC_SCHEMA_VERSION,
    AcceptanceSpec,
    AcceptanceSpecRegistry,
)
from opencognitiveos.verification.evidence_store import (
    ContentAddressedEvidenceStore,
    EvidenceIntegrityError,
    EvidenceKind,
    StoredEvidenceRecord,
)
from opencognitiveos.verification.manifest import (
    EVIDENCE_MANIFEST_SCHEMA_VERSION,
    EvidenceManifest,
    EvidenceManifestRegistry,
    ExecutionEnvironmentRecord,
    TrustedGitState,
    capture_trusted_git_state,
)
from opencognitiveos.verification.verifier import EFFECT_EVIDENCE_SCHEMA_VERSION, IndependentVerifier

__all__ = [
    "ACCEPTANCE_SPEC_SCHEMA_VERSION",
    "AcceptanceSpec",
    "AcceptanceSpecRegistry",
    "ContentAddressedEvidenceStore",
    "EFFECT_EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_MANIFEST_SCHEMA_VERSION",
    "EvidenceIntegrityError",
    "EvidenceKind",
    "EvidenceManifest",
    "EvidenceManifestRegistry",
    "ExecutionEnvironmentRecord",
    "IndependentVerifier",
    "StoredEvidenceRecord",
    "TrustedGitState",
    "capture_trusted_git_state",
]
