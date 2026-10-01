"""Shared fixtures and builders for the test suite."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.agents.a07_risk import DecisionContext, OpenPositionView, RiskManager
from trader.calendar import MacroCalendar, MarketCalendar, TradingWindows
from trader.config import Config, default_config
from trader.journal.store import SqlJournal
from trader.models.messages import Bar, Review, SetupTriggered
from trader.timeutil import ET, cents

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 10, 15)          # a Thursday; CPI day per the macro calendar is 10-14
PLAIN_DAY = date(2026, 10, 16)    # Friday, no macro event
ALL_PASS = {"MF-1": True, "MF-2": True, "MF-3": True, "MF-4": True, "MF-5": True}


def et(d: date, hhmm: str, sec: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m, sec), tzinfo=ET)


@pytest.fixture
def cfg() -> Config:
    return default_config()


@pytest.fixture
def cal() -> MarketCalendar:
    return MarketCalendar()


@pytest.fixture
def macro() -> MacroCalendar:
    return MacroCalendar.load(ROOT / "config" / "macro_calendar_2026_2027.yaml")


@pytest.fixture
def windows(cfg: Config, cal: MarketCalendar, macro: MacroCalendar) -> TradingWindows:
    return TradingWindows(cfg, cal, macro)


@pytest.fixture
def risk(cfg: Config, windows: TradingWindows) -> RiskManager:
    r = RiskManager(cfg, windows)
    r.start_day(PLAIN_DAY, Decimal("25000"))
    return r


@pytest.fixture
def journal(tmp_path: Path) -> SqlJournal:
    return SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")


def make_setup(ticker: str = "NVDA", *, setup: str = "ORB", window: str = "morning",
               style: str = "day", ts: datetime | None = None, trigger: str = "190.40",
               entry: str = "190.49", stop: str = "189.61", t1: str | None = None,
               t2: str | None = None, atr5: float = 0.90, resistance: float | None = 192.80,
               filters: dict[str, bool] | None = None, score: int = 75,
               context: dict[str, Any] | None = None, bar_closed: bool = True
               ) -> SetupTriggered:
    e, s = Decimal(entry), Decimal(stop)
    r = e - s
    return SetupTriggered(
        setup_id=f"t-{ticker}-{setup}-{(ts or et(PLAIN_DAY, '09:45')).isoformat()}",
        ts=ts or et(PLAIN_DAY, "09:45"), bar_closed=bar_closed, ticker=ticker,
        style=style, window=window, setup=setup,   # type: ignore[arg-type]
        trigger=Decimal(trigger), entry=e, stop=s, t1=Decimal(t1) if t1 else cents(e + r),
        t2=Decimal(t2) if t2 else cents(e + 2 * r), score=score, atr5=atr5, vwap=189.9,
        rvol=2.0, last_price=float(e), resistance=resistance,
        filters=filters or dict(ALL_PASS), context=context or {"atr14d": 4.0})


def make_ctx(now: datetime | None = None, *, equity: str = "25000", bp: str = "25000",
             last: str = "190.45", positions: list[OpenPositionView] | None = None,
             filters: dict[str, bool] | None = None, planned: bool = True,
             **kw: Any) -> DecisionContext:
    return DecisionContext(now=now or et(PLAIN_DAY, "09:45", 5), equity=Decimal(equity),
                           buying_power=Decimal(bp), last_price=Decimal(last),
                           positions=positions or [], filters=filters or dict(ALL_PASS),
                           planned=planned, **kw)


AGREE = Review(proposal_id="p1", verdict="agree", main_risk="none")
DISAGREE = Review(proposal_id="p1", verdict="disagree", main_risk="resistance overhead")


def bar(symbol: str, ts: datetime, o: float, h: float, l: float, c: float,  # noqa: E741
        v: int = 10000, tf: str = "1Min") -> Bar:
    return Bar(symbol=symbol, ts=ts.astimezone(UTC), timeframe=tf,   # type: ignore[arg-type]
               o=o, h=h, l=l, c=c, v=v)


def minute_series(symbol: str, d: date, start: str, closes: list[float], v: int = 10000,
                  spread: float = 0.05) -> list[Bar]:
    t0 = et(d, start)
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(bar(symbol, t0 + timedelta(minutes=i), o, max(o, c) + spread,
                       min(o, c) - spread, c, v))
        prev = c
    return out
