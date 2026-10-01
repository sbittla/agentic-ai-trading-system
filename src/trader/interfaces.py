"""Interfaces for every external dependency (INTERFACES.md §1, DV-2).

Agents depend only on these protocols; tests and replays swap in fakes.
Each I/O method has a timeout and raises one of the typed errors below (IF-4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from trader.calendar import Session
from trader.models.messages import (
    Account,
    ApprovalResponse,
    Bar,
    BracketRequest,
    BrokerPosition,
    Command,
    NewsItem,
    Order,
    OrderUpdate,
    Quote,
    TradingStatus,
)


class TraderError(Exception):
    """Base for typed I/O errors mapped to incidents by A10."""


class DataStale(TraderError):
    pass


class BrokerUnavailable(TraderError):
    pass


class BrokerRejected(TraderError):
    pass


class LLMTimeout(TraderError):
    pass


class LLMInvalidOutput(TraderError):
    pass


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...
    def is_trading_day(self, d: date) -> bool: ...
    def session(self, d: date) -> Session: ...
    async def sleep_until(self, t: datetime) -> None: ...


class MarketDataFeed(Protocol):
    async def subscribe(self, symbols: Sequence[str]) -> None: ...
    def bars(self) -> AsyncIterator[Bar]: ...
    def quotes(self) -> AsyncIterator[Quote]: ...
    def statuses(self) -> AsyncIterator[TradingStatus]: ...
    def last_message_at(self) -> datetime | None: ...


class HistoricalData(Protocol):
    async def daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]: ...
    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]: ...


class NewsSource(Protocol):
    name: str

    async def fetch(self, symbols: Sequence[str], since: datetime) -> list[NewsItem]: ...


class Broker(Protocol):
    async def account(self) -> Account: ...
    async def positions(self) -> list[BrokerPosition]: ...
    async def open_orders(self) -> list[Order]: ...
    async def submit_bracket(self, req: BracketRequest) -> Order: ...
    async def replace_stop(self, order_id: str, new_stop: Decimal) -> Order: ...
    async def cancel(self, order_id: str) -> None: ...
    async def close_position(self, symbol: str, qty: int | None = None,
                             limit_price: Decimal | None = None) -> Order: ...
    def order_updates(self) -> AsyncIterator[OrderUpdate]: ...
    async def clock_time(self) -> datetime: ...


class LLMResult(BaseModel):
    ok: bool
    value: Any | None = None
    error: str | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    raw: str = ""
    model: str = ""
    cached: bool = False


class LLMClient(Protocol):
    model: str

    async def structured(self, *, agent: str, prompt_version: str, system: str,
                         data: dict[str, Any], schema: type[BaseModel],
                         timeout_s: float = 60) -> LLMResult: ...
    async def healthy(self) -> bool: ...


class Notifier(Protocol):
    async def send(self, text: str, *, kind: str) -> None: ...
    async def request_approval(self, proposal_id: str, text: str, code: str,
                               timeout_s: int) -> ApprovalResponse: ...
    def commands(self) -> AsyncIterator[Command]: ...
