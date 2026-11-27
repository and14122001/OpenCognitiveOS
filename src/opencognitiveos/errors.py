"""Domain exceptions for OpenCognitiveOS M0-M1.2."""


class OpenCognitiveOSError(Exception):
    """Base exception for OpenCognitiveOS."""


class TaskConflictError(OpenCognitiveOSError):
    """Raised when repo_id::task_id exists with a different content_hash or idempotency conflict."""


class TaskNotFoundError(OpenCognitiveOSError):
    """Raised when a task or effect intent cannot be found."""


class InvalidTransitionError(OpenCognitiveOSError):
    """Raised when a task state transition violates the state machine."""


class CASConflictError(OpenCognitiveOSError):
    """Raised when expected revision does not match persisted revision."""


class StaleEpochError(OpenCognitiveOSError):
    """Raised when an operation uses an outdated fencing epoch."""


class LeaseConflictError(OpenCognitiveOSError):
    """Raised when another worker holds an active non-expired lease."""


class LeaseExpiredError(OpenCognitiveOSError):
    """Raised when a worker attempts an action with an expired lease."""


class PolicyDeniedError(OpenCognitiveOSError):
    """Raised when PolicyGate denies a provider, tool, path, or unauthorized transition."""


class BudgetExceededError(OpenCognitiveOSError):
    """Raised when retry or cost budget would be exceeded."""


class ReconciliationRequiredError(OpenCognitiveOSError):
    """Raised when an external effect has PENDING/UNKNOWN outcome and requires reconciliation."""


class VerificationFailedError(OpenCognitiveOSError):
    """Raised when independent verification fails or VerificationReceipt is missing/invalid."""


class DatabaseIntegrityError(OpenCognitiveOSError):
    """Raised when SQLite integrity or foreign key check fails."""
