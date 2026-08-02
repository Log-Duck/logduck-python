"""Data returned by the LogDuck API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["EventResponse"]


@dataclass(frozen=True)
class EventResponse:
    """An accepted event, as the API describes it back to us."""

    success: bool
    event_id: str
    time: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EventResponse:
        return cls(
            success=bool(payload.get("success", False)),
            event_id=str(payload.get("eventId", "")),
            time=str(payload.get("time", "")),
        )
