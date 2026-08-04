"""Official Python SDK for LogDuck — event logging and notifications.

::

    from logduck import LogDuckClient

    logduck = LogDuckClient(api_key="ld_...", source="checkout-api")
    logduck.send("order.placed", subject="order_9127", data={"total": 4999})
"""

from .client import AsyncLogDuckClient, LogDuckClient
from .errors import LogDuckError
from .models import EventResponse

#: Single source of truth for the version — pyproject.toml reads it from here,
#: and the release workflow checks the git tag against it.
__version__ = "2.0.0"

__all__ = [
    "AsyncLogDuckClient",
    "EventResponse",
    "LogDuckClient",
    "LogDuckError",
    "__version__",
]
