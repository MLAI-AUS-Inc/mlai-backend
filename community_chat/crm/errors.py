"""Typed contacts API failures, answered as ``{"error": code, "outcome": code}``."""


class CrmError(ValueError):
    """A stable error code, its HTTP status and any safe extra fields."""

    def __init__(self, code: str, status: int = 400, **extra):
        self.code, self.status, self.extra = code, status, extra
        super().__init__(code)
