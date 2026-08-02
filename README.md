# LogDuck Python SDK

Official Python SDK for [LogDuck](https://logduck.com) — send events from your
app and get notified about the ones that matter.

Python 3.9+. Sync and async clients, both built on `httpx`.

```bash
pip install logduck
```

## Quick start

Two settings are required: your API key, and a `source` naming the app sending
the event.

```python
import os
from logduck import LogDuckClient

logduck = LogDuckClient(api_key=os.environ["LOGDUCK_API_KEY"], source="checkout-api")

logduck.send("order.placed")
```

That is the whole minimal setup. Create an API key in your project settings —
see [Authentication](https://logduck.com/docs#authentication).

Async, so a web handler never blocks just to record that something happened:

```python
import os
from logduck import AsyncLogDuckClient

async with AsyncLogDuckClient(
    api_key=os.environ["LOGDUCK_API_KEY"], source="checkout-api"
) as logduck:
    await logduck.send("order.placed")
```

## Every option

Only `api_key` and `source` are required. The values below are the defaults you
get by omitting each one.

```python
import os
from datetime import datetime, timezone

from logduck import LogDuckClient

logduck = LogDuckClient(
    api_key=os.environ["LOGDUCK_API_KEY"],  # required — your key, starts with `ld_`
    source="checkout-api",                  # required — which app sent this, max 200 chars
    throw_on_error=False,                   # optional, default False — True raises instead of returning None
    retry_enabled=True,                     # optional, default True  — retry once on 5xx, network errors and 429
    max_retry_delay=10.0,                   # optional, default 10.0  — never block your caller longer than this
    timeout=30.0,                           # optional, default 30.0  — per-request timeout, seconds
    base_url="https://api.logduck.com",     # optional — override only for a self-hosted deployment
    # http_client=httpx.Client(),           # optional — bring your own; it is not closed for you
)

logduck.send(
    "order.placed",                                 # required — max 100 chars, naming rule below
    subject="order_9127",                           # optional — what the event is about, max 500 chars
    session_id="sess_8f21c",                        # optional — groups related events, max 256 chars
    time=datetime.now(timezone.utc),                # optional — defaults to when the server received it
    data={"total": 4999, "currency": "NOK"},        # optional — any JSON-serialisable mapping
    emoji="🛒",                                      # optional — shown next to the event, max 10 chars
)
```

One client per process is enough. Both clients support the context-manager
protocol, which releases the connection pool; call `close()` / `aclose()` if you
would rather manage it yourself.

**Naming `type`:** the server lowercases it and requires `^[a-z][a-z0-9_.]*$` —
letters, digits, `_` and `.`, starting with a letter. So `order.placed` and
`user.signup_completed` are fine; `order-placed` is rejected with a 400. Dots
are the conventional separator.

## Errors

By default `send` **never raises**. It returns `None` and logs a warning to the
`logduck` logger — a logging SDK that raises can take down the code it was only
meant to observe.

```python
result = logduck.send("order.placed")

if result is None:
    # The event did not reach LogDuck. A warning has already been logged.
    ...
```

Pass `throw_on_error=True` to get a `LogDuckError` instead. Note that `try` /
`except` only catches anything when that option is on — with the default, the
`except` below would never run:

```python
import os
import time

from logduck import LogDuckClient, LogDuckError

logduck = LogDuckClient(
    api_key=os.environ["LOGDUCK_API_KEY"],
    source="checkout-api",
    throw_on_error=True,  # required, or the except below never fires
)

# Your own queue — the SDK does not provide one.
retry_queue: list[tuple[dict, float]] = []

event = {"type": "order.placed", "subject": "order_9127"}

try:
    logduck.send(**event)
except LogDuckError as error:
    if error.retry_after is not None:
        # Rate limited for longer than the client will wait — queue it rather than lose it.
        retry_queue.append((event, time.time() + error.retry_after))
```

`LogDuckError` carries `status`, `body` and `retry_after`.

## Retries and rate limits

### The SDK sets `Idempotency-Key` for you

Every call generates an `Idempotency-Key` header, and reuses the **same** key on
the retry. The server deduplicates on it, so an event that timed out and got
retried is recorded once rather than twice.

You never set this header yourself. It is the main thing the SDK does for you
that raw HTTP does not — the endpoint *requires* the header (16–36 characters of
`[A-Za-z0-9_-]`) and rejects requests without one.

### What gets retried

| Response | Behaviour |
|---|---|
| 5xx, network failure, timeout | retried once after 1 second |
| 429 | retried once after the server's `Retry-After`, but only if that fits inside `max_retry_delay` |
| Any other 4xx | never retried — it will fail identically the second time |

The server rate-limits each API key in fixed one-minute windows, so `Retry-After`
can be as much as 60 seconds. Blocking your caller that long is rarely
acceptable, so beyond `max_retry_delay` the client gives up immediately and puts
the remaining wait on `error.retry_after`, letting you queue the event instead of
losing it.

## Using the API without the SDK

The SDK is a convenience, not a requirement — `POST /v1/events` is a plain JSON
endpoint over HTTPS, usable from any language or from `curl`:

```bash
curl -X POST https://api.logduck.com/v1/events \
  -H "X-API-Key: ld_your_key_here" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"type":"order.placed","source":"checkout-api","subject":"order_9127"}'
```

Full request and response reference:
**[logduck.com/docs#api-reference](https://logduck.com/docs#api-reference)**.

Going direct means you take on what the SDK handles: generating a valid
`Idempotency-Key` per event and reusing it across retries, honouring
`Retry-After` on a 429, and deciding which failures are worth retrying.

## Logging

Warnings go to the standard `logduck` logger. To quieten them:

```python
import logging

logging.getLogger("logduck").setLevel(logging.ERROR)
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
mypy
ruff check .
```

Releases are published by CI to PyPI via trusted publishing — no API token is
stored anywhere. Bump `__version__` in `src/logduck/__init__.py`, commit, then
tag: `git tag v1.1.0 && git push origin v1.1.0`. The workflow refuses the release
if the tag and `__version__` disagree.

## License

MIT
