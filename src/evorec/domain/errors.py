"""Application errors have no HTTP or storage dependencies."""


class HistoryConflict(Exception):
    """The captured history no longer matches the caller's expected version."""


class SnapshotMismatch(Exception):
    """A context or model result belongs to a different request snapshot."""


class UnreportedFallback(Exception):
    """A fixed strategy changed without explaining why."""


class ResourceNotFound(Exception):
    """A requested session or other domain resource does not exist."""


class AccessDenied(Exception):
    """A session credential is absent or does not match the stored owner."""


class IdempotencyConflict(Exception):
    """An idempotency key was already used for different semantic content."""


class IdempotencyInProgress(Exception):
    """An identical recommendation is still being computed."""


class IdempotencyReplay(Exception):
    """Return a previously completed recommendation without running it again."""

    def __init__(self, result: object):
        super().__init__("recommendation already completed")
        self.result = result


class SessionEpochConflict(Exception):
    """Feedback belongs to a request admitted before the current reset epoch."""


class FeedbackSourceMismatch(Exception):
    """Feedback does not refer to an item returned to the same session."""


class ManagementError(Exception):
    """Stable catalog or publication failure without storage details."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
