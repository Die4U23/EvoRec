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
