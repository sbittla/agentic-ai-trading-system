"""Alpaca market data: the IEX stream (A2-1) and REST history (A2-3, DQ-5).

One WebSocket connection carries bars, quotes and trading statuses for the 10 tickers plus
SPY, QQQ, SMH (13 of the 30 free symbols). Reconnects are reported so A2 can back-fill.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, time
from typing import Any

import structlog

from trader.interfaces import DataStale
from trader.models.messages import Bar, Quote, TradingStatus
from trader.timeutil import et_datetime

log = structlog.get_logger(__name__)
HALT_CODES = {"H", "T12", "H4", "H9", "H10", "H11", "LUDP", "LUDS", "MWC1", "MWC2", "MWC3",
              "MWC0", "IPO1", "IPOQ", "IPOE", "T3", "T5", "T6"}


def _bar(b: Any, timeframe: str = "1Min") -> Bar:
    ts: datetime = b.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return Bar(symbol=b.symbol, ts=ts, timeframe=timeframe,   # type: ignore[arg-type]
               o=float(b.open), h=float(b.high), l=float(b.low), c=float(b.close),
               v=int(b.volume), vwap=float(b.vwap) if b.vwap else None, source="iex")


class AlpacaIEXFeed:
    def __init__(self, key: str, secret: str, feed: str = "iex") -> None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream

        self.stream = StockDataStream(key, secret, feed=DataFeed.IEX if feed == "iex"
                                      else DataFeed.SIP)
        self._bars: asyncio.Queue[Bar] = asyncio.Queue()
        self._quotes: asyncio.Queue[Quote] = asyncio.Queue(maxsize=10000)
        self._status: asyncio.Queue[TradingStatus] = asyncio.Queue()
        self._last: datetime | None = None
        self.reconnects = 0
        self._task: asyncio.Task[None] | None = None

    def last_message_at(self) -> datetime | None:
        return self._last

    async def subscribe(self, symbols: Sequence[str]) -> None:
        async def on_bar(b: Any) -> None:
            self._last = datetime.now(UTC)
            await self._bars.put(_bar(b))

        async def on_quote(q: Any) -> None:
            self._last = datetime.now(UTC)
            item = Quote(symbol=q.symbol, ts=q.timestamp, bid=float(q.bid_price),
                         ask=float(q.ask_price), bid_size=int(q.bid_size or 0),
                         ask_size=int(q.ask_size or 0))
            if self._quotes.full():
                self._quotes.get_nowait()
            self._quotes.put_nowait(item)

        async def on_status(st: Any) -> None:
            code = str(getattr(st, "status_code", "") or "")
            kind = "halt" if code in HALT_CODES else "resume" if code in ("T",) else "luld"
            await self._status.put(TradingStatus(symbol=st.symbol, ts=st.timestamp,
                                                 kind=kind,   # type: ignore[arg-type]
                                                 detail=f"{code} {getattr(st, 'reason', '')}"))

        syms = list(symbols)
        self.stream.subscribe_bars(on_bar, *syms)
        self.stream.subscribe_quotes(on_quote, *syms)
        try:
            self.stream.subscribe_trading_statuses(on_status, *syms)
        except Exception as exc:   # noqa: BLE001 - not available on every feed/plan
            log.warning("statuses_unavailable", error=str(exc))
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.stream._run_forever()
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # noqa: BLE001
                self.reconnects += 1
                log.warning("stream_dropped", error=str(exc), reconnects=self.reconnects)
                await asyncio.sleep(min(30, 2 ** min(self.reconnects, 5)))

    async def bars(self) -> AsyncIterator[Bar]:
        while True:
            yield await self._bars.get()

    async def quotes(self) -> AsyncIterator[Quote]:
        while True:
            yield await self._quotes.get()

    async def statuses(self) -> AsyncIterator[TradingStatus]:
        while True:
            yield await self._status.get()

    async def close(self) -> None:
        await self.stream.stop_ws()
        if self._task:
            self._task.cancel()


class AlpacaHistory:
    """REST bars from the same IEX feed (DQ-2); under 200 calls/min."""

    def __init__(self, key: str, secret: str, feed: str = "iex", timeout_s: float = 30) -> None:
        from alpaca.data.historical import StockHistoricalDataClient

        self.client = StockHistoricalDataClient(key, secret)
        self.feed = feed
        self.timeout_s = timeout_s

    async def _bars(self, symbol: str, start: datetime, end: datetime, tf: str) -> list[Bar]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(symbol_or_symbols=symbol,
                               timeframe=TimeFrame.Day if tf == "1Day" else TimeFrame.Minute,
                               start=start, end=end, adjustment=Adjustment.SPLIT,
                               feed=DataFeed.IEX if self.feed == "iex" else DataFeed.SIP)
        try:
            res = await asyncio.wait_for(asyncio.to_thread(self.client.get_stock_bars, req),
                                         self.timeout_s)
        except TimeoutError as exc:
            raise DataStale(f"history timeout for {symbol}") from exc
        data = getattr(res, "data", {}) or {}
        return [_bar(b, tf) for b in data.get(symbol, [])]

    async def daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        bars = await self._bars(symbol, et_datetime(start, time(0, 0)),
                                et_datetime(end, time(0, 0)), "1Day")
        return [b for b in bars if b.ts.astimezone(UTC).date() < end]

    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        return await self._bars(symbol, start, end, "1Min")
