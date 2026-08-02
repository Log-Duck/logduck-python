"""Exceptions raised by the LogDuck client."""

from __future__ import annotations

__all__ = ["LogDuckError"]


class LogDuckError(Exception):
    """Raised when a request fails and ``throw_on_error`` is enabled.

    When it is not (the default), the same object is attached to the logged
    warning so the detail is still available to a log handler.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        #: HTTP status, or ``None`` when the request never got a response.
        self.status = status
        #: Raw response body, when there was one.
        self.body = body
        #: Set on a 429: how long the server asked us to wait, in seconds.
        #:
        #: Present so the application can queue the event and send it later
        #: rather than drop it — the client itself refuses to block for a full
        #: rate-limit window.
        self.retry_after = retry_after

    def __repr__(self) -> str:
        return (
            f"LogDuckError({self.message!r}, status={self.status!r}, "
            f"retry_after={self.retry_after!r})"
        )
