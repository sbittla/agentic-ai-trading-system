"""Hand-built market days for full-engine replays (deterministic, no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.backtest.engine import (
    InMemoryHistory,
    ReplayClock,
    ScriptedApprovals,
    _default_macro,
    _warm,
)
from trader.brokers.sim import SimBroker
from trader.calendar import MarketCalendar
from trader.config import Config
from trader.engine import TradingEngine
from trader.journal.store import SqlJournal
from trader.llm.fakes import RulesLLM
from trader.models.messages import Bar
from trader.timeutil import ET

PRICES = {"NVDA": 190.0, "AAPL": 250.0, "GOOGL": 240.0, "MSFT": 510.0, "AMZN": 230.0,
          "TSM": 290.0, "SPCX": 180.0, "META": 760.0, "AVGO": 340.0, "TSLA": 440.0,
          "SPY": 660.0, "QQQ": 600.0, "SMH": 310.0}


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET)


def mbar(sym: str, ts: datetime, o: float, c: float, v: int, spread: float = 0.05) -> Bar:
    return Bar(symbol=sym, ts=ts.astimezone(UTC), o=round(o, 2),
               h=round(max(o, c) + spread, 2), l=round(min(o, c) - spread, 2), c=round(c, 2),
               v=v)


def series(sym: str, d: date, closes: list[float], vol: int | list[int], start: tuple[int, int]
           = (9, 30), spread: float = 0.05) -> list[Bar]:
    t0 = et(d, *start)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        v = vol[i] if isinstance(vol, list) else vol
        out.append(mbar(sym, t0 + timedelta(minutes=i), prev, c, v, spread))
        prev = c
    return out


def flat(sym: str, d: date, px: float, drift: float = 0.0, vol: int = 1000) -> list[Bar]:
    return series(sym, d, [px + drift * i for i in range(390)], vol, spread=0.4 * px / 190)


def history(d: date, cal: MarketCalendar, n: int = 120) -> dict[str, list[Bar]]:
    days = []
    x = d
    while len(days) < n:
        x = cal.prev_trading_day(x)
        days.append(x)
    days.reverse()
    out: dict[str, list[Bar]] = {}
    for sym, px in PRICES.items():
        bars = []
        for i, dd in enumerate(days):
            c = px * (0.97 + 0.0003 * i) if sym != "NVDA" else 189.0
            bars.append(Bar(symbol=sym, ts=et(dd, 9, 30).astimezone(UTC), timeframe="1Day",
                            o=c, h=c * 1.02, l=c * 0.985, c=c, v=400_000))
        out[sym] = bars
    return out


def seed_minutes(d: date, cal: MarketCalendar, n: int = 20) -> list[Bar]:
    out: list[Bar] = []
    x = d
    for _ in range(n):
        x = cal.prev_trading_day(x)
        for sym, px in PRICES.items():
            out += flat(sym, x, px)
    return out


def orb_day(d: date, *, nvda_after: list[float] | None = None) -> list[Bar]:
    """NVDA: 5-min OR 190.0-190.3, breakout close 190.62 at 9:39 on volume, then the path in
    ``nvda_after`` (default: rally through T1 and T2). QQQ/SMH trend up. Others flat."""
    bars: list[Bar] = []
    for sym, px in PRICES.items():
        if sym == "NVDA":
            continue
        drift = 0.01 if sym in ("QQQ", "SMH", "SPY") else 0.0
        bars += flat(sym, d, px, drift)
    open_ = [190.0, 190.2, 190.3, 190.1, 190.2]
    brk = [190.25, 190.3, 190.4, 190.5, 190.62]
    retest = [190.55, 190.45, 190.5]          # pulls back to fill the 190.49 limit
    after = nvda_after if nvda_after is not None else (
        [190.6 + 0.08 * i for i in range(20)] + [192.2] * 361)
    closes = (open_ + brk + retest + after)[:390]
    closes += [closes[-1]] * (390 - len(closes))
    vols = [3000] * 5 + [4000] * 5 + [2500] * 380   # RVOL ~2.5-3 vs 1,000/min history
    bars += series("NVDA", d, closes, vols)
    bars.sort(key=lambda b: (b.ts, b.symbol))
    return bars


class Harness:
    """Drives a TradingEngine over hand-built bars, minute by minute."""

    def __init__(self, cfg: Config, d: date, tmp: Path, *, approvals: str = "yes",
                 llm: Any = None, equity: str = "100000",
                 journal_path: Path | None = None) -> None:
        self.cfg, self.d = cfg, d
        self.cal = MarketCalendar()
        self.clock = ReplayClock(et(d, 4, 0).astimezone(UTC))
        self.journal = SqlJournal(f"sqlite:///{(journal_path or tmp / 'j.db').as_posix()}")
        self.broker = SimBroker(self.clock.now, equity=Decimal(equity))
        self.notifier = ScriptedApprovals(approvals)   # type: ignore[arg-type]
        self.llm = llm or RulesLLM()
        self.engine = TradingEngine(
            cfg, now=self.clock.now, calendar=self.cal, macro=_default_macro(cfg),
            broker=self.broker, journal=self.journal, llm=self.llm, notifier=self.notifier,
            history=InMemoryHistory(history(d, self.cal)), require_quotes=False,
            reports_dir=tmp / "reports", sim_mode=True)
        for comp in ("alpaca", "data", "ollama", "postgres", "clock", "reconcile"):
            self.engine.sup.set_health(comp, "green", "test", self.clock.now())

    async def start(self, seed: bool = True) -> None:
        await self.engine.start()
        if seed:
            _warm(self.engine, seed_minutes(self.d, self.cal))

    async def premarket(self) -> None:
        for hh, mm in ((7, 0), (8, 0), (9, 0)):
            await self.at(hh, mm)

    async def at(self, hh: int, mm: int) -> None:
        self.clock.advance_to(et(self.d, hh, mm).astimezone(UTC))
        await self.engine.tick(self.clock.now())
        await self.engine.settle()
        await self.updates()

    async def updates(self) -> None:
        for u in self.broker.drain_updates():
            await self.engine.on_order_update(u)

    async def run_bars(self, bars: list[Bar], until: datetime | None = None) -> None:
        from itertools import groupby

        for ts, grp in groupby(bars, key=lambda b: b.ts):
            if until is not None and ts >= until.astimezone(UTC):
                break
            batch = list(grp)
            self.clock.advance_to(ts + timedelta(minutes=1))
            for b in batch:
                await self.engine.on_bar(b)
            for b in batch:
                self.broker.match(b)
            await self.updates()
            await self.engine.tick(self.clock.now())
            await self.engine.settle()
            await self.updates()

    def remaining(self, bars: list[Bar], after: datetime) -> list[Bar]:
        return [b for b in bars if b.ts >= after.astimezone(UTC)]
