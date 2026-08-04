"""Behaviour of the sync and async clients.

The two share their policy but not their plumbing, so the cases that matter —
retries, rate limits, idempotency — are exercised against both rather than
assumed to follow from the shared module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from logduck import AsyncLogDuckClient, LogDuckClient, LogDuckError

BASE_URL = "https://api.logduck.com"
ENDPOINT = f"{BASE_URL}/v1/events"


def accepted() -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": True, "eventId": "evt_1", "time": "2026-08-02T09:00:00.000Z"},
    )


def rate_limited(retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.Response(429, json={"error": "Rate limit exceeded"}, headers=headers)


@pytest.fixture
def client() -> LogDuckClient:
    # No sleeping in tests: retries are asserted by call count, and a real
    # 1s delay per retry would dominate the suite.
    return LogDuckClient(api_key="ld_test_key", source="test-suite", max_retry_delay=0)


@pytest.fixture
def strict_client() -> LogDuckClient:
    return LogDuckClient(
        api_key="ld_test_key", source="test-suite", throw_on_error=True, max_retry_delay=0
    )


# -- the request it builds -------------------------------------------------


@respx.mock
def test_posts_to_v1_events_with_auth_headers(client: LogDuckClient) -> None:
    route = respx.post(ENDPOINT).mock(return_value=accepted())

    client.send("user.signup")

    assert route.called
    request = route.calls[0].request
    assert request.headers["x-api-key"] == "ld_test_key"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"].startswith("LogDuck-Python/")


@respx.mock
def test_sends_an_idempotency_key_the_server_will_accept(client: LogDuckClient) -> None:
    # The server enforces ^[a-zA-Z0-9_-]{16,36}$ and rejects the request
    # outright without it, so this is part of the contract, not a nicety.
    import re

    route = respx.post(ENDPOINT).mock(return_value=accepted())

    client.send("user.signup")

    key = route.calls[0].request.headers["idempotency-key"]
    assert re.fullmatch(r"[a-zA-Z0-9_-]{16,36}", key)


@respx.mock
def test_source_comes_from_the_client_not_the_event() -> None:
    route = respx.post(ENDPOINT).mock(return_value=accepted())
    client = LogDuckClient(api_key="ld_k", source="checkout-api")

    client.send("order.placed")

    assert json.loads(route.calls[0].request.content)["source"] == "checkout-api"


@respx.mock
def test_serialises_a_datetime_to_iso_8601(client: LogDuckClient) -> None:
    route = respx.post(ENDPOINT).mock(return_value=accepted())

    client.send("user.signup", time=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc))

    assert json.loads(route.calls[0].request.content)["time"] == "2026-08-02T09:00:00+00:00"


@respx.mock
def test_omits_absent_fields_rather_than_sending_null(client: LogDuckClient) -> None:
    route = respx.post(ENDPOINT).mock(return_value=accepted())

    client.send("user.signup")

    body = json.loads(route.calls[0].request.content)
    assert "subject" not in body
    assert "emoji" not in body


@respx.mock
def test_returns_the_parsed_response(client: LogDuckClient) -> None:
    respx.post(ENDPOINT).mock(return_value=accepted())

    result = client.send("user.signup")

    assert result is not None
    assert result.success is True
    assert result.event_id == "evt_1"


# -- validation ------------------------------------------------------------


@respx.mock
def test_rejects_an_empty_type_without_touching_the_network(client: LogDuckClient) -> None:
    route = respx.post(ENDPOINT).mock(return_value=accepted())

    assert client.send("   ") is None
    assert not route.called


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("type", {"type": "a" * 101}),
        ("subject", {"type": "ok", "subject": "a" * 501}),
        ("session_id", {"type": "ok", "session_id": "a" * 257}),
        ("message", {"type": "ok", "message": "a" * 501}),
        ("emoji", {"type": "ok", "emoji": "a" * 11}),
    ],
)
@respx.mock
def test_rejects_over_long_fields(field: str, kwargs: dict, client: LogDuckClient) -> None:
    route = respx.post(ENDPOINT).mock(return_value=accepted())

    assert client.send(**kwargs) is None
    assert not route.called


@respx.mock
def test_sends_cloudevents_wire_names(client: LogDuckClient) -> None:
    """The wire names are the contract, and they are not the kwarg names.

    A CloudEvents extension attribute name must be lowercase alphanumeric, so
    ``session_id`` goes out as ``sessionid``. The server discards what it does
    not recognise rather than erroring, so getting this wrong loses the field
    silently - which is why the snake_case spelling is asserted absent too.
    """
    route = respx.post(ENDPOINT).mock(return_value=accepted())

    client.send(
        "order.placed",
        subject="order_9127",
        session_id="sess_8f21c",
        message="Order placed on the Pro plan",
    )

    body = json.loads(route.calls[0].request.content)
    assert body["sessionid"] == "sess_8f21c"
    assert body["message"] == "Order placed on the Pro plan"
    assert "session_id" not in body
    assert "sessionId" not in body
    assert "metadata" not in body


@respx.mock
def test_omits_message_and_sessionid_when_unset(client: LogDuckClient) -> None:
    route = respx.post(ENDPOINT).mock(return_value=accepted())

    client.send("order.placed")

    body = json.loads(route.calls[0].request.content)
    assert "message" not in body
    assert "sessionid" not in body


def test_flags_an_api_key_without_the_ld_prefix() -> None:
    assert LogDuckClient(api_key="oops", source="s").validate_configuration() is False


def test_accepts_a_well_formed_configuration(client: LogDuckClient) -> None:
    assert client.validate_configuration() is True


# -- failure handling ------------------------------------------------------


@respx.mock
def test_returns_none_instead_of_raising_by_default(client: LogDuckClient) -> None:
    # A logging SDK must not take down the code it was meant to observe.
    respx.post(ENDPOINT).mock(return_value=httpx.Response(401, text="nope"))

    assert client.send("user.signup") is None


@respx.mock
def test_raises_when_throw_on_error_is_set(strict_client: LogDuckClient) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(401, text="nope"))

    with pytest.raises(LogDuckError) as excinfo:
        strict_client.send("user.signup")

    assert excinfo.value.status == 401


@respx.mock
def test_does_not_retry_a_4xx(client: LogDuckClient) -> None:
    # A validation error will fail identically the second time; retrying it
    # only doubles the load.
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(400, text="bad"))

    client.send("user.signup")

    assert route.call_count == 1


@respx.mock
def test_retries_a_5xx_once_and_succeeds(client: LogDuckClient) -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[httpx.Response(503, text="boom"), accepted()]
    )

    result = client.send("user.signup")

    assert result is not None and result.success
    assert route.call_count == 2


@respx.mock
def test_retries_a_network_failure_once(client: LogDuckClient) -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[httpx.ConnectError("connection refused"), accepted()]
    )

    result = client.send("user.signup")

    assert result is not None and result.success
    assert route.call_count == 2


@respx.mock
def test_reuses_the_same_idempotency_key_across_a_retry(client: LogDuckClient) -> None:
    # This is what makes retrying safe: the server deduplicates on the key, so
    # a retry after a timeout cannot double-post the event.
    route = respx.post(ENDPOINT).mock(
        side_effect=[httpx.Response(503, text="boom"), accepted()]
    )

    client.send("user.signup")

    first = route.calls[0].request.headers["idempotency-key"]
    second = route.calls[1].request.headers["idempotency-key"]
    assert first == second


@respx.mock
def test_sends_exactly_once_when_retries_are_disabled() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(503, text="boom"))
    client = LogDuckClient(api_key="ld_k", source="s", retry_enabled=False)

    assert client.send("user.signup") is None
    assert route.call_count == 1


@respx.mock
def test_a_malformed_success_body_is_a_failure_not_a_crash(client: LogDuckClient) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, text="not json"))

    assert client.send("user.signup") is None


# -- rate limits -----------------------------------------------------------


@respx.mock
def test_waits_for_retry_after_and_retries_within_the_cap() -> None:
    route = respx.post(ENDPOINT).mock(side_effect=[rate_limited("0"), accepted()])
    client = LogDuckClient(api_key="ld_k", source="s", max_retry_delay=10)

    result = client.send("user.signup")

    assert result is not None and result.success
    assert route.call_count == 2


@respx.mock
def test_gives_up_immediately_when_retry_after_exceeds_the_cap() -> None:
    # The server rate-limits in one-minute windows, so Retry-After can be 60s.
    # Blocking a caller that long is worse than dropping the event.
    route = respx.post(ENDPOINT).mock(return_value=rate_limited("45"))
    client = LogDuckClient(api_key="ld_k", source="s", throw_on_error=True, max_retry_delay=10)

    with pytest.raises(LogDuckError) as excinfo:
        client.send("user.signup")

    assert excinfo.value.status == 429
    assert excinfo.value.retry_after == 45.0
    assert route.call_count == 1


@respx.mock
def test_surfaces_retry_after_so_the_caller_can_queue_the_event() -> None:
    respx.post(ENDPOINT).mock(return_value=rate_limited("30"))
    client = LogDuckClient(api_key="ld_k", source="s", throw_on_error=True, max_retry_delay=10)

    with pytest.raises(LogDuckError) as excinfo:
        client.send("user.signup")

    assert excinfo.value.retry_after == 30.0


@respx.mock
def test_assumes_a_full_window_when_retry_after_is_missing() -> None:
    # Retrying immediately would add load to a server that just asked us to
    # back off, so the missing case must not round down to zero.
    respx.post(ENDPOINT).mock(return_value=rate_limited())
    client = LogDuckClient(api_key="ld_k", source="s", throw_on_error=True, max_retry_delay=10)

    with pytest.raises(LogDuckError) as excinfo:
        client.send("user.signup")

    assert excinfo.value.retry_after == 60.0


# -- the async client ------------------------------------------------------


@respx.mock
async def test_async_client_sends_an_event() -> None:
    route = respx.post(ENDPOINT).mock(return_value=accepted())

    async with AsyncLogDuckClient(api_key="ld_k", source="async-suite") as client:
        result = await client.send("user.signup", subject="user_42")

    assert result is not None and result.event_id == "evt_1"
    assert json.loads(route.calls[0].request.content)["subject"] == "user_42"


@respx.mock
async def test_async_client_retries_a_5xx_once() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[httpx.Response(503, text="boom"), accepted()]
    )

    async with AsyncLogDuckClient(api_key="ld_k", source="s", max_retry_delay=0) as client:
        result = await client.send("user.signup")

    assert result is not None and result.success
    assert route.call_count == 2


@respx.mock
async def test_async_client_reuses_the_idempotency_key_across_a_retry() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[httpx.Response(503, text="boom"), accepted()]
    )

    async with AsyncLogDuckClient(api_key="ld_k", source="s", max_retry_delay=0) as client:
        await client.send("user.signup")

    assert (
        route.calls[0].request.headers["idempotency-key"]
        == route.calls[1].request.headers["idempotency-key"]
    )


@respx.mock
async def test_async_client_surfaces_retry_after() -> None:
    respx.post(ENDPOINT).mock(return_value=rate_limited("45"))

    async with AsyncLogDuckClient(
        api_key="ld_k", source="s", throw_on_error=True, max_retry_delay=10
    ) as client:
        with pytest.raises(LogDuckError) as excinfo:
            await client.send("user.signup")

    assert excinfo.value.retry_after == 45.0


@respx.mock
async def test_async_client_returns_none_instead_of_raising_by_default() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(401, text="nope"))

    async with AsyncLogDuckClient(api_key="ld_k", source="s") as client:
        assert await client.send("user.signup") is None
