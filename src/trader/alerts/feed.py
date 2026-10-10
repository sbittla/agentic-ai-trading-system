"""Thin Alpaca market-data client for the alerts service.

Talks to Alpaca directly (``websockets`` + ``httpx``) instead of through ``alpaca-py``: raw JSON
frames are handed straight to the engine with no pydantic model per message and no pandas
import, which keeps the process small and the per-trade cost low.

Free plan limits: IEX feed, 30 symbols, one market-data WebSocket per account (so do not run
this and ``trader-core`` on the same Alpaca account at the same time).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)
DATA_URL = "https://data.alpaca.markets"
STREAM_URL = "wss://stream.data.alpaca.markets/v2/{feed}"
TRADING_URLS = ("https://paper-api.alpaca.markets", "https://api.alpaca.markets")


class AuthError(Exception):
    pass


def parse_ts(text: str) -> datetime:
    """RFC 3339 with up to nanoseconds ("2026-10-09T13:31:00.123456789Z") -> aware UTC."""
    s = text.rstrip("Z")
    tz = "+00:00"
    for sep in ("+", "-"):
        i = s.rfind(sep)
        if i > 10:   # an offset, not a date separator
            s, tz = s[:i], s[i:]
            break
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s + tz).astimezone(UTC)


class AlpacaRest:
    def __init__(self, key: str, secret: str, feed: str = "iex",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.feed = feed
        self._client = httpx.AsyncClient(
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=20, transport=transport)
        self._trading_url: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        r = await self._client.get(url, params=params)
        if r.status_code in (401, 403):
            raise AuthError(f"{r.status_code} from {url.split('?')[0]}")
        r.raise_for_status()
        return r.json()

    async def clock(self) -> dict[str, Any]:
        """``{"is_open", "next_open", "next_close", "timestamp"}`` (works with paper or live
        keys; the first URL that accepts them is remembered)."""
        urls = [self._trading_url] if self._trading_url else list(TRADING_URLS)
        err: Exception | None = None
        for base in urls:
            try:
                data: dict[str, Any] = await self._get(f"{base}/v2/clock")
                self._trading_url = base
                return data
            except AuthError as exc:
                err = exc
        raise err or AuthError("clock unavailable")

    async def snapshots(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        syms = ",".join(symbols)
        if not syms:
            return {}
        data: dict[str, dict[str, Any]] = await self._get(
            f"{DATA_URL}/v2/stocks/snapshots", {"symbols": syms, "feed": self.feed})
        return {k: v for k, v in data.items() if v}

    async def minute_bars(self, symbols: Iterable[str], start: datetime,
                          end: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
        """Today's 1-minute bars for many symbols in as few calls as possible."""
        params: dict[str, Any] = {"symbols": ",".join(symbols), "timeframe": "1Min",
                                  "start": start.astimezone(UTC).isoformat(),
                                  "feed": self.feed, "limit": 10000, "adjustment": "raw"}
        if end is not None:
            params["end"] = end.astimezone(UTC).isoformat()
        out: dict[str, list[dict[str, Any]]] = {}
        for _ in range(20):   # page cap: 20 x 10k bars is far beyond one day of 30 symbols
            data = await self._get(f"{DATA_URL}/v2/stocks/bars", params)
            for sym, bars in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out


def prev_close_from_snapshot(snap: dict[str, Any], today_et: str) -> float | None:
    """Prior session close. Before the open the snapshot's ``dailyBar`` is still yesterday's."""
    daily = snap.get("dailyBar") or {}
    prev = snap.get("prevDailyBar") or {}
    if daily.get("t"):
        from trader.timeutil import to_et

        if to_et(parse_ts(daily["t"])).date().isoformat() != today_et:
            return float(daily["c"])
    return float(prev["c"]) if prev.get("c") else None


def last_price_from_snapshot(snap: dict[str, Any]) -> float | None:
    for key, field in (("latestTrade", "p"), ("minuteBar", "c"), ("dailyBar", "c")):
        v = (snap.get(key) or {}).get(field)
        if v:
            return float(v)
    return None


Handler = Callable[[list[dict[str, Any]]], Awaitable[None]]


class AlpacaStream:
    """One WebSocket for 1-minute bars and trades; reconnects with back-off."""

    def __init__(self, key: str, secret: str, feed: str = "iex",
                 connect: Callable[..., Any] | None = None) -> None:
        self._key, self._secret = key, secret
        self.url = STREAM_URL.format(feed=feed)
        self._connect = connect
        self._ws: Any = None
        self.bar_symbols: set[str] = set()
        self.trade_symbols: set[str] = set()
        self.last_message: float | None = None
        self.reconnects = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def set_symbols(self, bars: Iterable[str], trades: Iterable[str] = ()) -> None:
        """Bars for every watched symbol; trades (the busy channel) only where a price rule
        needs tick-level speed."""
        new_b, new_t = set(bars), set(trades)
        msgs = []
        for action, b, t in (("unsubscribe", self.bar_symbols - new_b,
                              self.trade_symbols - new_t),
                             ("subscribe", new_b - self.bar_symbols,
                              new_t - self.trade_symbols)):
            if b or t:
                msgs.append({"action": action, "bars": sorted(b), "trades": sorted(t)})
        self.bar_symbols, self.trade_symbols = new_b, new_t
        if self._ws is not None:
            for m in msgs:
                await self._ws.send(json.dumps(m))

    async def run(self, handler: Handler) -> None:
        """Stream until ``stop()``; raises AuthError on bad keys (no point retrying)."""
        connect = self._connect
        if connect is None:
            import websockets

            connect = websockets.connect
        self._stop.clear()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with connect(self.url, max_size=2**22, ping_interval=20,
                                   close_timeout=5) as ws:
                    await self._auth(ws)
                    self._ws = ws
                    if self.bar_symbols or self.trade_symbols:
                        await ws.send(json.dumps({"action": "subscribe",
                                                  "bars": sorted(self.bar_symbols),
                                                  "trades": sorted(self.trade_symbols)}))
                    backoff = 1.0
                    await self._pump(ws, handler)
            except AuthError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # noqa: BLE001 - network: reconnect
                if self._stop.is_set():
                    break
                self.reconnects += 1
                log.warning("alerts_stream_dropped", error=str(exc)[:200],
                            reconnects=self.reconnects)
            finally:
                self._ws = None
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(30.0, backoff * 2)

    async def _auth(self, ws: Any) -> None:
        await ws.send(json.dumps({"action": "auth", "key": self._key,
                                  "secret": self._secret}))
        for _ in range(3):
            for m in json.loads(await asyncio.wait_for(ws.recv(), 15)):
                if m.get("T") == "success" and m.get("msg") == "authenticated":
                    return
                if m.get("T") == "error":
                    code = m.get("code")
                    if code in (402, 404, 409):   # auth failed / timeout / insufficient plan
                        raise AuthError(f"alpaca stream {code}: {m.get('msg')}")
                    raise ConnectionError(f"alpaca stream {code}: {m.get('msg')}")
        raise ConnectionError("alpaca stream: no auth response")

    async def _pump(self, ws: Any, handler: Handler) -> None:
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            while True:
                recv = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait({recv, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    recv.cancel()
                    return
                self.last_message = time.monotonic()
                msgs = json.loads(recv.result())
                for m in msgs:
                    if m.get("T") == "error":
                        log.warning("alerts_stream_error", code=m.get("code"), msg=m.get("msg"))
                await handler(msgs)
        finally:
            stop.cancel()
