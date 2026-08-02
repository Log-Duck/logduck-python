"""Sync and async clients for posting events to LogDuck."""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Optional, Union

import httpx

from ._policy import Decision, Fail, Retry, Succeed, decide_response, decide_transport_error
from .errors import LogDuckError
from .models import EventResponse

__all__ = ["LogDuckClient", "AsyncLogDuckClient"]

DEFAULT_BASE_URL = "https://api.logduck.com"

_LIMITS = {
    "type": 100,
    "source": 200,
    "subject": 500,
    "session_id": 256,
    "emoji": 10,
}

logger = logging.getLogger("logduck")


class _BaseClient:
    """Configuration, validation and payload building.

    The two clients differ only in how they perform a request and how they
    sleep; everything else lives here or in :mod:`logduck._policy`.
    """

    def __init__(
        self,
        api_key: str,
        source: str,
        *,
        throw_on_error: bool = False,
        retry_enabled: bool = True,
        max_retry_delay: float = 10.0,
        timeout: float = 30.0,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        from . import __version__

        self.api_key = api_key or ""
        self.source = source or ""
        self.throw_on_error = throw_on_error
        self.retry_enabled = retry_enabled
        self.max_retry_delay = max_retry_delay
        self.timeout = timeout
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._user_agent = f"LogDuck-Python/{__version__}"

        if not self.api_key:
            logger.warning("LogDuck API key is not configured - all requests will fail")
        if not self.source:
            logger.warning("LogDuck source is not configured - all requests will fail")

    # -- configuration -----------------------------------------------------

    def validate_configuration(self) -> bool:
        """Check the configuration without sending anything.

        Returns ``False`` and logs a warning for each problem, which makes it
        useful as a startup assertion.
        """
        valid = True

        if not self.api_key:
            logger.warning("LogDuck validation failed: API key is not configured")
            valid = False
        elif not self.api_key.startswith("ld_"):
            logger.warning("LogDuck validation failed: API key must start with 'ld_'")
            valid = False

        if not self.source:
            logger.warning("LogDuck validation failed: source is not configured")
            valid = False
        elif len(self.source) > _LIMITS["source"]:
            logger.warning(
                "LogDuck validation failed: source exceeds %d characters", _LIMITS["source"]
            )
            valid = False

        return valid

    # -- request construction ---------------------------------------------

    def _validate_event(
        self,
        type: str,
        subject: Optional[str],
        session_id: Optional[str],
        emoji: Optional[str],
    ) -> Optional[str]:
        if not type or not type.strip():
            return "type is required"
        if len(type) > _LIMITS["type"]:
            return f"type exceeds {_LIMITS['type']} characters"
        if not self.source:
            return "source is not configured"
        if len(self.source) > _LIMITS["source"]:
            return f"source exceeds {_LIMITS['source']} characters"
        if subject is not None and len(subject) > _LIMITS["subject"]:
            return f"subject exceeds {_LIMITS['subject']} characters"
        if session_id is not None and len(session_id) > _LIMITS["session_id"]:
            return f"session_id exceeds {_LIMITS['session_id']} characters"
        if emoji is not None and len(emoji) > _LIMITS["emoji"]:
            return f"emoji exceeds {_LIMITS['emoji']} characters"
        return None

    def _payload(
        self,
        type: str,
        subject: Optional[str],
        session_id: Optional[str],
        time: Optional[Union[datetime, str]],
        data: Optional[Mapping[str, Any]],
        emoji: Optional[str],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": type, "source": self.source}

        # Absent fields are omitted rather than sent as null, so the payload
        # stays honest about what the caller actually set.
        if subject is not None:
            payload["subject"] = subject
        if session_id is not None:
            payload["sessionId"] = session_id
        if time is not None:
            payload["time"] = time.isoformat() if isinstance(time, datetime) else time
        if data is not None:
            payload["data"] = dict(data)
        if emoji is not None:
            payload["emoji"] = emoji

        return payload

    def _headers(self, idempotency_key: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "Idempotency-Key": idempotency_key,
            "User-Agent": self._user_agent,
        }

    @property
    def _url(self) -> str:
        return f"{self.base_url}/v1/events"

    @property
    def _max_attempts(self) -> int:
        return 2 if self.retry_enabled else 1

    # -- outcome handling --------------------------------------------------

    def _fail(self, error: LogDuckError, warning: str) -> None:
        """Raise, or log and carry on, depending on ``throw_on_error``.

        Callers return ``None`` after this rather than returning its result:
        it either raises or produces nothing, and pretending otherwise hides
        that from the type checker.
        """
        if self.throw_on_error:
            raise error
        logger.warning("LogDuck %s", warning, exc_info=error)

    def _parse(self, body: str, status: int) -> Optional[EventResponse]:
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as exc:
            self._fail(
                LogDuckError(f"Failed to parse response: {exc}", status=status, body=body),
                "event failed: could not parse response",
            )
            return None
        return EventResponse.from_payload(payload)

    def _decide(self, response: httpx.Response, attempt: int) -> Decision:
        return decide_response(
            status=response.status_code,
            headers=response.headers,
            body=response.text,
            attempt=attempt,
            max_attempts=self._max_attempts,
            max_retry_delay=self.max_retry_delay,
        )


def _idempotency_key() -> str:
    """The server requires 16-36 characters of ``[A-Za-z0-9_-]``.

    A UUID is 36 with hyphens, so it fits exactly.
    """
    return str(uuid.uuid4())


class LogDuckClient(_BaseClient):
    """Sends events to LogDuck.

    ::

        logduck = LogDuckClient(api_key="ld_...", source="checkout-api")
        logduck.send("order.placed", subject="order_9127")

    One client per process is enough. Use it as a context manager, or call
    :meth:`close`, to release the underlying connection pool.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        client: Optional[httpx.Client] = kwargs.pop("http_client", None)
        super().__init__(*args, **kwargs)
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=self.timeout)

    def send(
        self,
        type: str,  # shadows the builtin, but it is the wire field name and reads well as a kwarg
        *,
        subject: Optional[str] = None,
        session_id: Optional[str] = None,
        time: Optional[Union[datetime, str]] = None,
        data: Optional[Mapping[str, Any]] = None,
        emoji: Optional[str] = None,
    ) -> Optional[EventResponse]:
        """Send one event.

        Returns the created event, or ``None`` when the send failed and
        ``throw_on_error`` is off (the default).
        """
        error = self._validate_event(type, subject, session_id, emoji)
        if error is not None:
            self._fail(LogDuckError(error, status=400), f"validation failed: {error}")
            return None

        # One key for the whole call, reused across retries: that is what makes
        # a retry safe, since the server deduplicates on it.
        key = _idempotency_key()
        payload = self._payload(type, subject, session_id, time, data, emoji)

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._http.post(
                    self._url, json=payload, headers=self._headers(key), timeout=self.timeout
                )
            except httpx.HTTPError as exc:
                decision = decide_transport_error(
                    exc=exc, attempt=attempt, max_attempts=self._max_attempts
                )
            else:
                decision = self._decide(response, attempt)
                if isinstance(decision, Succeed):
                    return self._parse(response.text, response.status_code)

            if isinstance(decision, Retry):
                logger.warning("LogDuck %s", decision.reason)
                _time.sleep(decision.delay)
                continue

            assert isinstance(decision, Fail)
            self._fail(decision.error, decision.warning)
            return None

        return None  # pragma: no cover - the loop always returns

    def close(self) -> None:
        """Close the connection pool, unless it was passed in."""
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> LogDuckClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class AsyncLogDuckClient(_BaseClient):
    """The async counterpart of :class:`LogDuckClient`.

    ::

        async with AsyncLogDuckClient(api_key="ld_...", source="api") as logduck:
            await logduck.send("order.placed")

    Exists so an async web handler never has to make a blocking HTTP call just
    to record that something happened.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        client: Optional[httpx.AsyncClient] = kwargs.pop("http_client", None)
        super().__init__(*args, **kwargs)
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=self.timeout)

    async def send(
        self,
        type: str,  # shadows the builtin, but it is the wire field name and reads well as a kwarg
        *,
        subject: Optional[str] = None,
        session_id: Optional[str] = None,
        time: Optional[Union[datetime, str]] = None,
        data: Optional[Mapping[str, Any]] = None,
        emoji: Optional[str] = None,
    ) -> Optional[EventResponse]:
        """Send one event. See :meth:`LogDuckClient.send`."""
        error = self._validate_event(type, subject, session_id, emoji)
        if error is not None:
            self._fail(LogDuckError(error, status=400), f"validation failed: {error}")
            return None

        key = _idempotency_key()
        payload = self._payload(type, subject, session_id, time, data, emoji)

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._http.post(
                    self._url, json=payload, headers=self._headers(key), timeout=self.timeout
                )
            except httpx.HTTPError as exc:
                decision = decide_transport_error(
                    exc=exc, attempt=attempt, max_attempts=self._max_attempts
                )
            else:
                decision = self._decide(response, attempt)
                if isinstance(decision, Succeed):
                    return self._parse(response.text, response.status_code)

            if isinstance(decision, Retry):
                logger.warning("LogDuck %s", decision.reason)
                await asyncio.sleep(decision.delay)
                continue

            assert isinstance(decision, Fail)
            self._fail(decision.error, decision.warning)
            return None

        return None  # pragma: no cover - the loop always returns

    async def aclose(self) -> None:
        """Close the connection pool, unless it was passed in."""
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncLogDuckClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()
