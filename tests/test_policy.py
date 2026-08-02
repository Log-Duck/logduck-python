"""The retry rules, tested without any HTTP.

These are the decisions both clients defer to, so pinning them here is what
keeps the sync and async paths from diverging.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from logduck._policy import (
    RETRY_AFTER_FALLBACK_SECONDS,
    Fail,
    Retry,
    Succeed,
    decide_response,
    parse_retry_after,
)


def decide(status: int, *, attempt: int = 1, headers: dict | None = None, cap: float = 10.0):
    return decide_response(
        status=status,
        headers=headers or {},
        body="",
        attempt=attempt,
        max_attempts=2,
        max_retry_delay=cap,
    )


def test_2xx_succeeds() -> None:
    assert isinstance(decide(200), Succeed)


def test_5xx_retries_while_attempts_remain() -> None:
    assert isinstance(decide(503, attempt=1), Retry)


def test_5xx_fails_once_attempts_run_out() -> None:
    assert isinstance(decide(503, attempt=2), Fail)


def test_4xx_never_retries() -> None:
    # It will fail identically the second time; retrying only doubles the load.
    for status in (400, 401, 403, 404, 422):
        assert isinstance(decide(status, attempt=1), Fail), status


def test_429_retries_when_the_wait_is_within_the_cap() -> None:
    decision = decide(429, headers={"Retry-After": "5"}, cap=10)

    assert isinstance(decision, Retry)
    assert decision.delay == 5.0


def test_429_gives_up_when_the_wait_exceeds_the_cap() -> None:
    # The server rate-limits in one-minute windows. Blocking a caller for a
    # full window is worse than dropping the event, so the failure carries
    # retry_after and lets the application queue it instead.
    decision = decide(429, headers={"Retry-After": "45"}, cap=10)

    assert isinstance(decision, Fail)
    assert decision.error.retry_after == 45.0


def test_header_lookup_is_case_insensitive() -> None:
    decision = decide(429, headers={"retry-after": "5"}, cap=10)

    assert isinstance(decision, Retry)
    assert decision.delay == 5.0


def test_missing_retry_after_assumes_a_full_window() -> None:
    assert parse_retry_after(None) == RETRY_AFTER_FALLBACK_SECONDS


def test_unparseable_retry_after_assumes_a_full_window() -> None:
    # Anything else would risk rounding down to zero and hammering a server
    # that just asked us to back off.
    assert parse_retry_after("soon") == RETRY_AFTER_FALLBACK_SECONDS


def test_retry_after_accepts_delta_seconds() -> None:
    assert parse_retry_after("30") == 30.0


def test_retry_after_accepts_an_http_date() -> None:
    # The spec allows either form. Treating the date form as unparseable would
    # fall back to a full minute and delay events needlessly.
    when = datetime.now(timezone.utc) + timedelta(seconds=45)
    header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")

    assert 43 <= parse_retry_after(header) <= 46


def test_a_retry_after_in_the_past_is_clamped_to_zero() -> None:
    when = datetime.now(timezone.utc) - timedelta(seconds=30)
    header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")

    assert parse_retry_after(header) == 0.0
