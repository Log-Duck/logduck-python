"""What to do about a response, decided once for both clients.

The sync and async clients differ only in how they perform a request and how
they sleep. Keeping the *policy* — what counts as retryable, how long to wait,
what error to raise — in one place is what stops the two drifting apart, and
makes the rules testable without any HTTP at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Union

from .errors import LogDuckError

__all__ = ["Retry", "Fail", "Succeed", "Decision", "decide_response", "decide_transport_error"]

#: Delay before retrying a 5xx or a network failure.
TRANSIENT_RETRY_SECONDS = 1.0

#: The server rate-limits in fixed one-minute windows, so a missing or
#: unparseable ``Retry-After`` cannot mean less than a full window. Assume the
#: maximum rather than retrying immediately and adding load to a server that
#: just asked us to back off.
RETRY_AFTER_FALLBACK_SECONDS = 60.0


@dataclass(frozen=True)
class Retry:
    """Wait ``delay`` seconds, then send the same request again."""

    delay: float
    reason: str


@dataclass(frozen=True)
class Fail:
    """Give up. ``error`` is raised or logged depending on ``throw_on_error``."""

    error: LogDuckError
    warning: str


@dataclass(frozen=True)
class Succeed:
    """The response is a 2xx; the caller parses the body."""


Decision = Union[Retry, Fail, Succeed]


def decide_response(
    *,
    status: int,
    headers: Mapping[str, str],
    body: str,
    attempt: int,
    max_attempts: int,
    max_retry_delay: float,
) -> Decision:
    """Classify an HTTP response."""
    if 200 <= status < 300:
        return Succeed()

    # 429 is the one 4xx worth retrying: the server tells us when its window
    # resets, and the unchanged Idempotency-Key makes the retry safe.
    if status == 429:
        retry_after = parse_retry_after(_get_header(headers, "retry-after"))

        if attempt < max_attempts and retry_after <= max_retry_delay:
            return Retry(retry_after, f"rate limited, retrying in {retry_after:.0f}s")

        # Out of attempts, or the wait is longer than a caller should be
        # blocked for. Surface retry_after so the event can be queued.
        return Fail(
            LogDuckError(
                f"Rate limit exceeded. Retry after {retry_after:.0f}s.",
                status=status,
                body=body,
                retry_after=retry_after,
            ),
            f"event dropped: rate limit exceeded, retry after {retry_after:.0f}s",
        )

    # Any other 4xx is our fault and will fail identically next time.
    if 400 <= status < 500:
        message = _client_error_message(status)
        return Fail(
            LogDuckError(message, status=status, body=body),
            f"event failed: {message}",
        )

    if attempt < max_attempts:
        return Retry(
            TRANSIENT_RETRY_SECONDS,
            f"request failed with status {status}, retrying",
        )

    return Fail(
        LogDuckError(
            f"Request failed with status {status} after {max_attempts} attempts",
            status=status,
            body=body,
        ),
        f"event failed after {max_attempts} attempts",
    )


def decide_transport_error(
    *, exc: Exception, attempt: int, max_attempts: int
) -> Decision:
    """Classify a network failure or timeout — no response ever arrived."""
    if attempt < max_attempts:
        return Retry(TRANSIENT_RETRY_SECONDS, f"request failed ({exc}), retrying")

    return Fail(
        LogDuckError(f"{exc} (after {max_attempts} attempts)"),
        f"event failed after {max_attempts} attempts: {exc}",
    )


def parse_retry_after(header: Optional[str]) -> float:
    """Seconds to wait, from either form the spec allows.

    ``Retry-After`` may be delta-seconds or an HTTP-date. Treating the date
    form as unparseable would fall back to a full minute and drop events that
    were only seconds away from being sendable.
    """
    if not header:
        return RETRY_AFTER_FALLBACK_SECONDS

    try:
        return max(0.0, float(header))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return RETRY_AFTER_FALLBACK_SECONDS

    if when is None:
        return RETRY_AFTER_FALLBACK_SECONDS

    # An HTTP-date without a timezone is UTC by definition.
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _get_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive lookup, since a plain dict is allowed here in tests."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _client_error_message(status: int) -> str:
    return {
        400: "Validation error",
        401: "Invalid API key",
        403: "API key expired or unauthorized",
    }.get(status, f"Request failed with status {status}")
