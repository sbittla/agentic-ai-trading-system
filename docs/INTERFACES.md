# Interface Contracts

**Version:** 1.0 (September 30, 2026) · Companion to [ARCHITECTURE.md](ARCHITECTURE.md)

Every external dependency sits behind a small Python interface (`typing.Protocol`). Agents depend only on these interfaces, so tests and replays swap in fakes without touching agent code. Each interface has one real implementation and one fake.

| Interface | Real implementation | Fake for tests and replay |
| --- | --- | --- |
| `Clock` | `MarketClock` (system time, NTP-checked, Alpaca calendar) | `ReplayClock` (driven by recorded bars) |
| `MarketDataFeed` | `AlpacaIEXFeed` | `RecordedFeed` (Parquet) |
| `HistoricalData` | `AlpacaHistory` | `ParquetHistory` |
| `NewsSource` | `FinnhubNews`, `EdgarFilings`, `RedditPosts` | `RecordedNews` |
| `Broker` | `AlpacaBroker` | `SimBroker` (see BACKTEST_ENGINE.md) |
| `LLMClient` | `OllamaClient` | `CachedLLM` (responses keyed by input hash) |
| `Notifier` | `WhatsAppNotifier` | `ConsoleNotifier` |
| `Journal` | `SqlJournal` on Postgres | `SqlJournal` on a per-run SQLite file |
| `OutboxBus` | `PgOutboxBus` (Postgres `LISTEN/NOTIFY`) | `InMemoryOutbox` |

---

## 1. Interfaces

```python
from typing import Protocol, AsyncIterator, Sequence
from datetime import datetime, date
from decimal import Decimal

class Clock(Protocol):
    def now(self) -> datetime: ...                       # timezone-aware, ET
    def is_trading_day(self, d: date) -> bool: ...
    def session(self, d: date) -> "Session": ...          # open, close, early_close flag
    async def sleep_until(self, t: datetime) -> None: ...

class MarketDataFeed(Protocol):
    async def subscribe(self, symbols: Sequence[str]) -> None: ...
    def bars(self) -> AsyncIterator["Bar"]: ...           # closed 1-min bars
    def quotes(self) -> AsyncIterator["Quote"]: ...
    def last_message_at(self) -> datetime | None: ...     # for staleness (A2-4)

class HistoricalData(Protocol):
    async def daily_bars(self, symbol: str, start: date, end: date) -> list["Bar"]: ...
    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list["Bar"]: ...

class NewsSource(Protocol):
    async def fetch(self, symbols: Sequence[str], since: datetime) -> list["NewsItem"]: ...

class Broker(Protocol):
    async def account(self) -> "Account": ...             # equity, buying power, status
    async def positions(self) -> list["Position"]: ...
    async def open_orders(self) -> list["Order"]: ...
    async def submit_bracket(self, req: "BracketRequest") -> "Order": ...
    async def replace_stop(self, order_id: str, new_stop: Decimal) -> "Order": ...
    async def cancel(self, order_id: str) -> None: ...
    async def close_position(self, symbol: str, qty: int | None = None) -> "Order": ...
    def order_updates(self) -> AsyncIterator["OrderUpdate"]: ...   # fills, cancels, rejects

class LLMClient(Protocol):
    async def structured(self, *, agent: str, prompt_version: str, system: str,
                         data: dict, schema: type["BaseModel"],
                         timeout_s: float = 60) -> "LLMResult": ...    # parsed model or error
    async def healthy(self) -> bool: ...

class Notifier(Protocol):
    async def send(self, text: str, *, kind: str) -> None: ...
    async def request_approval(self, proposal: "TradeProposal", code: str,
                               timeout_s: int) -> "ApprovalResponse": ...
    def commands(self) -> AsyncIterator["Command"]: ...   # authenticated (SECURITY.md SEC-1..3)
    # Real implementation polls Twilio's Messages API (ADR-013); no inbound webhook.

class OutboxBus(Protocol):
    async def publish(self, channel: str, payload: "BaseModel") -> int: ...   # returns outbox id
    def subscribe(self, channel: str) -> AsyncIterator["OutboxMessage"]: ...
    async def ack(self, outbox_id: int) -> None: ...
```

## 2. Core data types (Pydantic)

```python
class BracketRequest(BaseModel):
    client_order_id: str          # f"{proposal_id}-{leg}", max 48 chars
    symbol: str
    qty: conint(ge=1)             # whole shares (Alpaca brackets)
    limit_price: Decimal
    take_profit: Decimal
    stop_price: Decimal
    time_in_force: Literal["day", "gtc"]

class LLMResult(BaseModel):
    ok: bool
    value: BaseModel | None
    error: str | None
    latency_ms: int
    input_tokens: int
    output_tokens: int
    raw: str                      # stored in llm_calls
```

Other types (`Bar`, `Quote`, `NewsItem`, `Account`, `Position`, `Order`, `OrderUpdate`, `Command`, `ApprovalResponse`, `TradeProposal`) are defined in `src/trader/models/` as listed in [DATA_SPEC.md](DATA_SPEC.md#4-message-types-event-bus).

## 3. Contract rules

- **IF-1:** All money values are `Decimal`, never `float`.
- **IF-2:** All timestamps are timezone-aware; stored in UTC, compared in ET.
- **IF-3:** `Broker.submit_bracket` is idempotent on `client_order_id`: a repeat returns the existing order.
- **IF-4:** Every interface method that does I/O has a timeout and raises a typed error (`DataStale`, `BrokerUnavailable`, `LLMTimeout`, `LLMInvalidOutput`) that A10 maps to an incident.
- **IF-5:** Fakes must pass the same contract test suite as the real implementations (`tests/contract/`).
- **IF-6:** Only `AlpacaBroker` imports `alpaca-py` trading classes; only `OllamaClient` imports the HTTP client for Ollama (ARCHITECTURE.md §2).

## 4. Build files

| File | Purpose |
| --- | --- |
| [`docker/docker-compose.yml`](../docker/docker-compose.yml) | Six services (postgres, ollama, trader-core, news-sentiment, notifier, backup) plus replay, dev and monitoring profiles |
| [`docker/Dockerfile`](../docker/Dockerfile) | One multi-stage file with targets `core`, `news`, `notifier`, `tools`, `dev` |
| [`docker/postgres/init.sql`](../docker/postgres/init.sql) | Per-service roles and the outbox bus table |
| [`tools/devcontainer.json`](../tools/devcontainer.json) | VS Code dev container; copy to `.devcontainer/devcontainer.json` |
| [`pyproject.toml`](../pyproject.toml) | Package metadata, pinned dependencies, tool config |
| [`tools/ci.yml`](../tools/ci.yml) | Lint, type check, unit and contract tests, secret scan on every push. Move to `.github/workflows/ci.yml` to enable |
| `.env.<name>.example` | One template per container env file (`.env.core`, `.env.news`, `.env.notifier`, …), no values; `trader init-env` creates the real files |
