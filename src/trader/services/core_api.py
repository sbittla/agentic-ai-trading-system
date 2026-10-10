"""trader-core internal read-only HTTP API for the owner console (UI_REQUIREMENTS UI-4, UI-5,
UI-22, UI-30, UI-31, OP-16).

- GET only, JSON only, on the internal Docker network (port 8081 is never published).
- Nothing here changes state: every write from the console goes through the outbox
  (``inbound_cmd``, source ``console``), the same path as Telegram (UI-C2).
- Live values the journal does not hold: in-memory bars, quotes, filters, the checklist
  dry-run, the scanner, latency, option chains. Quotes for symbols outside the universe come
  from Alpaca REST snapshots (cached 5 s), so the console needs no broker keys.

The HTTP layer is a few lines of ``asyncio.start_server`` (the core image has no web
framework); ``CoreApi.handle`` holds the logic and is what the tests call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import structlog

from trader.timeutil import et_date, et_datetime, to_et

log = structlog.get_logger(__name__)
SYMBOL = re.compile(r"^[A-Z][A-Z.]{0,5}$")
QUOTE_TTL_S = 5
BARS_TTL_S = 30
DAILY_TTL_S = 600


def _bar(b: Any) -> dict[str, Any]:
    return {"t": b.ts.isoformat(), "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v}


def _jsonable(x: Any) -> Any:
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, datetime | date):
        return x.isoformat()
    if isinstance(x, set | tuple):
        return list(x)
    return str(x)


class CoreApi:
    def __init__(
        self, engine: Any, rest: Any = None, now: Callable[[], datetime] | None = None
    ) -> None:
        self.engine = engine
        self.rest = rest
        self.now = now or engine.now
        self._cache: dict[tuple[str, str], tuple[datetime, Any]] = {}

    async def _cached(self, key: tuple[str, str], ttl: float, fn: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        now = self.now()
        if hit and (now - hit[0]).total_seconds() < ttl:
            return hit[1]
        val = await fn()
        self._cache[key] = (now, val)
        return val

    # -- dispatch ------------------------------------------------------------------------------
    async def handle(self, path: str, params: dict[str, str]) -> tuple[int, Any]:
        sym = params.get("symbol", "").upper()
        if "symbol" in params and not SYMBOL.match(sym):
            return 400, {"error": "bad symbol"}
        routes = {
            "/health": lambda: self._health(),
            "/status": lambda: self._status(),
            "/quote": lambda: self.quote(sym),
            "/bars": lambda: self.bars(sym, params.get("tf", "1Min")),
            "/daily": lambda: self.daily(sym),
            "/checklist": lambda: self.checklist(sym),
            "/scanner": lambda: self.scanner(),
            "/latency": lambda: self._latency(),
            "/options/iv": lambda: self.options_iv(sym),
            "/options/chain": lambda: self.options_chain(sym, params.get("expiry")),
        }
        fn = routes.get(path)
        if fn is None:
            return 404, {"error": "not found"}
        try:
            return 200, await fn()
        except LookupError as exc:
            return 404, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the console shows what is missing (UI-27)
            log.warning("core_api_error", path=path, error=str(exc))
            return 503, {"error": str(exc)[:200]}

    async def _health(self) -> dict[str, Any]:
        return {"ok": True}

    async def _status(self) -> dict[str, Any]:
        d: dict[str, Any] = self.engine.status_dict()
        return d

    # -- UI-4 quote, bars ------------------------------------------------------------------------
    def in_universe(self, sym: str) -> bool:
        return sym in self.engine.cfg.universe.tickers or sym in self.engine.md.state

    async def quote(self, sym: str) -> dict[str, Any]:
        st = self.engine.md.state.get(sym)
        if st is not None and st.last_price is not None:
            q = st.quote
            return {
                "symbol": sym,
                "source": "stream",
                "last": st.last_price,
                "prior_close": st.prior_close,
                "change_pct": st.day_change_pct(),
                "vwap": st.vwap_value,
                "rvol": st.rvol(),
                "volume": st.cum_vol,
                "bid": q.bid if q else None,
                "ask": q.ask if q else None,
                "spread_pct": q.spread_pct if q else None,
                "hod": st.hod,
                "lod": st.lod,
                "halted": st.halted,
                "universe": sym in self.engine.cfg.universe.tickers,
            }
        if self.rest is None:
            raise LookupError(f"{sym}: no stream data and no REST client")

        async def fetch() -> dict[str, Any]:
            snaps = await self.rest.snapshots([sym])
            if sym not in snaps:
                raise LookupError(f"{sym}: unknown symbol")
            sn = snaps[sym]
            last = (sn.get("latestTrade") or {}).get("p")
            prev = (sn.get("prevDailyBar") or {}).get("c")
            lq = sn.get("latestQuote") or {}
            bid, ask = lq.get("bp"), lq.get("ap")
            mid = (bid + ask) / 2 if bid and ask else None
            return {
                "symbol": sym,
                "source": "snapshot",
                "last": last,
                "prior_close": prev,
                "change_pct": (last / prev - 1) * 100 if last and prev else None,
                "vwap": (sn.get("dailyBar") or {}).get("vw"),
                "rvol": None,
                "volume": (sn.get("dailyBar") or {}).get("v"),
                "bid": bid,
                "ask": ask,
                "spread_pct": (ask - bid) / mid * 100 if mid and ask and bid else None,
                "hod": (sn.get("dailyBar") or {}).get("h"),
                "lod": (sn.get("dailyBar") or {}).get("l"),
                "halted": False,
                "universe": False,
            }

        out: dict[str, Any] = await self._cached(("quote", sym), QUOTE_TTL_S, fetch)
        return out

    async def bars(self, sym: str, tf: str) -> dict[str, Any]:
        st = self.engine.md.state.get(sym)
        lv = self.engine.ta.levels.get(sym)
        levels = (
            {
                k: getattr(lv, k)
                for k in (
                    "prior_high",
                    "prior_low",
                    "prior_close",
                    "pm_high",
                    "pm_low",
                    "ema20d",
                    "atr14d",
                )
            }
            if lv is not None
            else {}
        )
        if st is not None and st.bars1:
            bars = st.bars5 if tf == "5Min" else st.bars1
            vw = st.vwap_at_5m if tf == "5Min" else []
            return {
                "symbol": sym,
                "tf": tf,
                "bars": [_bar(b) for b in bars],
                "vwap": vw,
                "or": st.or15 or st.or5,
                "levels": levels,
            }
        if self.rest is None:
            raise LookupError(f"{sym}: no bars")
        day = et_date(self.now())
        start = (
            et_datetime(day, self.engine.windows.cal.session(day).open.time())
            if self.engine.cal.is_trading_day(day)
            else self.now() - timedelta(days=1)
        )

        async def fetch() -> list[dict[str, Any]]:
            data = await self.rest.minute_bars([sym], start)
            return [
                {"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
                for b in data.get(sym, [])
            ]

        rows = await self._cached(("bars", sym), BARS_TTL_S, fetch)
        return {"symbol": sym, "tf": "1Min", "bars": rows, "vwap": [], "or": None, "levels": levels}

    async def daily(self, sym: str) -> dict[str, Any]:
        hist = self.engine.history
        if hist is None:
            raise LookupError("no history client")
        day = et_date(self.now())

        async def fetch() -> list[dict[str, Any]]:
            return [_bar(b) for b in await hist.daily_bars(sym, day - timedelta(days=200), day)]

        return {"symbol": sym, "bars": await self._cached(("daily", sym), DAILY_TTL_S, fetch)}

    # -- UI-5 checklist dry-run ------------------------------------------------------------------
    async def checklist(self, sym: str) -> dict[str, Any]:
        eng = self.engine
        now = self.now()
        if sym not in eng.cfg.universe.tickers:
            return {
                "symbol": sym,
                "tradable": False,
                "note": "informational: not tradable (UR-1)",
                "items": [],
            }
        st = eng.md.state[sym]
        f = eng.cfg.filters
        filt = eng.ta.filters(sym, now, "ORB", 75)
        qqq = eng.md.state.get("QQQ")
        smh = eng.md.state.get("SMH")
        win, why = eng.windows.entry_window(now, "day")
        comp = eng.compliance.check_entry(sym, now, "day")
        rs = eng.risk.state
        items = [
            {
                "id": "window",
                "passed": win is not None,
                "value": win or why,
                "threshold": "inside an entry window (TW-1..TW-6)",
            },
            {
                "id": "MF-1",
                "passed": filt["MF-1"],
                "value": f"QQQ above VWAP: {qqq.above_vwap() if qqq else 'n/a'}",
                "threshold": "QQQ above VWAP (or the A+ exception)",
            },
            {
                "id": "MF-2",
                "passed": filt["MF-2"],
                "value": f"SMH above VWAP: {smh.above_vwap() if smh else 'n/a'}"
                if sym in f.smh_check_for
                else "not a semi",
                "threshold": "SMH above VWAP (semis)",
            },
            {
                "id": "MF-3",
                "passed": filt["MF-3"],
                "value": st.rvol(),
                "threshold": f"RVOL >= {f.min_rvol}",
            },
            {
                "id": "MF-4",
                "passed": filt["MF-4"],
                "value": st.quote.spread_pct if st.quote else None,
                "threshold": f"spread < {f.max_spread_pct}%",
            },
            {
                "id": "MF-5",
                "passed": filt["MF-5"],
                "value": "halt/stale/incomplete check",
                "threshold": f"no halt in {f.halt_lookback_min} min, data fresh",
            },
            {
                "id": "MF-6",
                "passed": filt["MF-6"],
                "value": "two-sided quote",
                "threshold": f"quote <= {eng.cfg.data.quote_max_age_s}s old",
            },
            {
                "id": "compliance",
                "passed": comp.ok,
                "value": comp.reason or "ok",
                "threshold": "no wash-sale / earnings / streak lock",
            },
            {
                "id": "limits",
                "passed": not (
                    rs.kill_switch
                    or rs.halted
                    or "day" in rs.paused_styles
                    or rs.trades_total_today >= eng.cfg.risk.max_trades_per_day
                    or sym in rs.news_blocked
                ),
                "value": f"trades {rs.trades_total_today}/{eng.cfg.risk.max_trades_per_day}, "
                f"kill {rs.kill_switch}, halted {rs.halted or 'no'}, "
                f"paused {sorted(rs.paused_styles)}, news-blocked "
                f"{sym in rs.news_blocked}",
                "threshold": "LL-1..LL-7, DR, NW-2",
            },
        ]
        planned = [
            i.model_dump(mode="json")
            for p in eng.strategy.plans.values()
            for i in p.watch
            if i.ticker == sym and p.day == et_date(now)
        ]
        return {
            "symbol": sym,
            "tradable": True,
            "ts": now.isoformat(),
            "items": items,
            "planned": planned,
            "regime": eng.regime,
        }

    # -- UI-30 scanner, UI-31 latency -------------------------------------------------------------
    async def scanner(self) -> dict[str, Any]:
        eng = self.engine
        names = list(eng.cfg.universe.tickers) + [
            r["ticker"] for r in eng.in_play_today if r["ticker"] not in eng.cfg.universe.tickers
        ]
        rows = []
        for t in names:
            st = eng.md.state.get(t)
            if st is None or st.last_price is None:
                rows.append({"ticker": t, "in_play": t not in eng.cfg.universe.tickers})
                continue
            vw = st.vwap_value
            rows.append(
                {
                    "ticker": t,
                    "last": st.last_price,
                    "change_pct": st.day_change_pct(),
                    "rvol": st.rvol(),
                    "rs_qqq": eng.ta.rel_strength(st),
                    "vwap_dist_pct": (st.last_price / vw - 1) * 100 if vw else None,
                    "in_play": t not in eng.cfg.universe.tickers,
                }
            )
        rows.sort(key=lambda r: -abs(r.get("change_pct") or 0))
        return {"ts": self.now().isoformat(), "rows": rows}

    async def _latency(self) -> dict[str, Any]:
        lv, detail = self.engine.latency.health()
        return {
            "summary": self.engine.latency.summary(),
            "health": lv,
            "detail": detail,
            "prearmed": len(self.engine.prearm.entries),
        }

    # -- OP-16 options -----------------------------------------------------------------------------
    def _desk(self) -> Any:
        if self.engine.options is None:
            raise LookupError("options module is not running (options.mode off)")
        return self.engine.options

    async def options_iv(self, sym: str) -> dict[str, Any]:
        desk = self._desk()
        sp = desk.spot(sym)
        if sp is None:
            raise LookupError(f"{sym}: no price")
        ctx, _ = await desk.data.context(sym, sp, to_et(self.now()).date())
        return {
            "symbol": sym,
            "spot": sp,
            "atm_iv_30d": ctx.atm_iv_30d,
            "iv_rank": ctx.iv_rank,
            "iv_pct": ctx.iv_pct,
            "history": ctx.history_label or "full",
            "term_front": ctx.term_front,
            "term_30d": ctx.term_30d,
            "skew": ctx.skew,
            "expected_moves": {e.isoformat(): m for e, m in sorted(ctx.expected_moves.items())},
            "text": ctx.text(),
        }

    async def options_chain(self, sym: str, expiry: str | None) -> dict[str, Any]:
        desk = self._desk()
        sp = desk.spot(sym)
        if sp is None:
            raise LookupError(f"{sym}: no price")
        today = to_et(self.now()).date()
        qs = await desk.data.quotes(sym, sp, today, (0, 60), band=0.08)
        exps = sorted({q.expiry for q in qs})
        if not exps:
            raise LookupError(f"{sym}: no chain")
        want = date.fromisoformat(expiry) if expiry else None
        exp = (
            min(exps, key=lambda e: abs((e - want).days))
            if want
            else next((e for e in exps if (e - today).days >= desk.ocfg.day_dte[0]), exps[-1])
        )
        from trader.options.strategist import liquid

        rows = []
        for q in sorted((q for q in qs if q.expiry == exp), key=lambda q: (q.strike, q.kind)):
            ok, why = liquid(q, desk.ocfg, self.now())
            rows.append(
                {
                    "symbol": q.symbol,
                    "kind": q.kind,
                    "strike": q.strike,
                    "bid": q.bid,
                    "ask": q.ask,
                    "mid": q.mid,
                    "iv": q.iv,
                    "delta": q.delta,
                    "oi": q.open_interest,
                    "volume": q.volume,
                    "liquid": ok,
                    "why": why,
                }
            )
        return {
            "symbol": sym,
            "spot": sp,
            "expiry": exp.isoformat(),
            "expiries": [e.isoformat() for e in exps],
            "rows": rows,
        }

    # -- HTTP --------------------------------------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                pass
            parts = line.split()
            if len(parts) < 2 or parts[0] != "GET":
                status, body = 405, {"error": "GET only"}
            else:
                u = urlsplit(parts[1])
                params = {k: v[0] for k, v in parse_qs(u.query).items()}
                status, body = await self.handle(u.path, params)
            data = json.dumps(body, default=_jsonable).encode()
            writer.write(
                f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 - one bad client must not stop the server
            log.info("core_api_client_error", error=str(exc))
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def serve(self, host: str = "0.0.0.0", port: int = 8081) -> None:  # noqa: S104
        """Bound inside the container; the port is not published (internal network only)."""
        server = await asyncio.start_server(self._client, host, port)
        log.info("core_api_listening", port=port)
        async with server:
            await server.serve_forever()
