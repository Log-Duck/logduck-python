# LogDuck Python SDK

Official Python SDK for [LogDuck](https://logduck.com) — send events from your
app and get notified about the ones that matter.

Python 3.9+. Sync and async clients, both built on `httpx`.

```bash
pip install logduck
```

## Usage

```python
from logduck import LogDuckClient

logduck = LogDuckClient(api_key=os.environ["LOGDUCK_API_KEY"], source="checkout-api")

logduck.send(
    "order.placed",
    subject="order_9127",
    data={"total": 4999, "currency": "NOK"},
    emoji="🛒",
)
```

Async, so a web handler never blocks just to record that something happened:

```python
from logduck import AsyncLogDuckClient

async with AsyncLogDuckClient(api_key=..., source="checkout-api") as logduck:
    await logduck.send("order.placed", subject="order_9127")
```

One client per process is enough. Both support the context-manager protocol,
which releases the underlying connection pool; call `close()` / `aclose()` if
you would rather manage it yourself.

## Options

| Option | Default | What it does |
|---|---|---|
| `api_key` | — | Required. Must start with `ld_`. |
| `source` | — | Required. Identifies the app sending events (max 200 chars). Attached to every event. |
| `throw_on_error` | `False` | When false, failures are logged and `send` returns `None`. |
| `retry_enabled` | `True` | Retry once on 5xx, network errors and rate limits. |
| `max_retry_delay` | `10.0` | Longest wait, in seconds, before retrying a rate-limited request. |
| `timeout` | `30.0` | Request timeout in seconds. |
| `base_url` | `https://api.logduck.com` | Override for a self-hosted deployment. |
| `http_client` | — | Bring your own `httpx.Client` / `httpx.AsyncClient`. It is not closed for you. |

## Event fields

| Argument | Required | Limit |
|---|---|---|
| `type` | yes | 100 chars. Lowercased by the server, which requires `^[a-z][a-z0-9_.]*$` — letters, digits, `_` and `.`, starting with a letter. A hyphen is rejected. |
| `subject` | no | 500 chars. The resource the event is about. |
| `session_id` | no | 256 chars. Groups related events. |
| `time` | no | `datetime` or ISO 8601 string. Defaults to server time. |
| `data` | no | Any JSON-serialisable mapping. |
| `emoji` | no | 10 chars. |

`source` is not an event field — it comes from the client.

## Errors

`send` returns `None` on failure and logs a warning to the `logduck` logger. A
logging SDK that raises can take down the code it was only meant to observe, so
that is the default; pass `throw_on_error=True` to get a `LogDuckError` instead.

```python
from logduck import LogDuckError

try:
    logduck.send("order.placed")
except LogDuckError as error:
    if error.retry_after:
        # Rate limited. Queue it rather than lose it.
        queue.append((event, time.time() + error.retry_after))
```

### Retries and rate limits

Every request carries an `Idempotency-Key`, unchanged across a retry, so the
server deduplicates rather than double-posting.

- **5xx and network failures** — retried once after 1 second.
- **429** — retried once, waiting for the period the server gives in
  `Retry-After`, but never longer than `max_retry_delay`. The server limits each
  API key in fixed one-minute windows, so `Retry-After` can be as much as 60
  seconds; blocking a caller that long is rarely acceptable. When the wait would
  exceed the cap the client gives up immediately and the error carries
  `retry_after`, so the event can be queued instead of lost.
- **Other 4xx** — never retried. They will fail identically the second time.

## Logging

Warnings go to the standard `logduck` logger. To silence them:

```python
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
tag: `git tag v1.1.0 && git push origin v1.1.0`. The workflow refuses the
release if the tag and `__version__` disagree.

## License

MIT
