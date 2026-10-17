"""A1 Universe agent (A1-1, A1-2, UR-1..UR-5).

The owner's list lives in config and never changes at runtime. A1 rejects off-list tickers,
stores the list with its effective date (so backtests use the list active at the time),
blocks tickers that fail the tradability/history check, and reports the weekly top-10
comparison without ever editing the list.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import structlog
from sqlalchemy import select

from trader.config import Config
from trader.journal import schema as s
from trader.journal.store import SqlJournal

log = structlog.get_logger(__name__)

# Candidates for the weekly market-cap comparison (A1-2); the owner's list is always included.
MEGA_CAP_CANDIDATES = ("NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "TSM", "SPCX", "META", "AVGO",
                       "TSLA", "BRK.B", "LLY", "JPM", "WMT", "V", "ORCL", "MA", "XOM", "NFLX",
                       "COST", "AMD", "PLTR", "ASML", "UNH", "JNJ", "HD", "BAC", "ABBV")


class UniverseAgent:
    def __init__(self, cfg: Config, journal: SqlJournal | None,
                 now: Callable[[], datetime]) -> None:
        self.cfg = cfg
        self.journal = journal
        self.now = now
        self.blocked: dict[str, str] = {}

    def check(self, ticker: str) -> tuple[bool, str]:
        t = ticker.upper()
        if t not in self.cfg.universe.tickers:
            canon = self.cfg.canonical(t)
            why = (f"{t} is another share class of {canon} (UR-2)" if canon != t
                   else f"{t} is not on the owner's list (UR-1)")
            log.warning("off_list_ticker", ticker=t, reason=why)
            return False, why
        if t in self.blocked:
            return False, self.blocked[t]
        return True, ""

    def record(self, effective: date) -> None:
        """UR-5: store the list with its effective date (once per change)."""
        if self.journal is None:
            return
        current = self.list_for(effective)
        if sorted(current) == sorted(self.cfg.universe.tickers):
            return
        self.journal.insert_many(s.universe, [
            {"effective_date": effective, "ticker": t, "rank": i + 1, "market_cap": None,
             "on_owner_list": True} for i, t in enumerate(self.cfg.universe.tickers)])

    def list_for(self, d: date) -> list[str]:
        """The owner's list that was active on ``d`` (UR-5)."""
        if self.journal is None:
            return list(self.cfg.universe.tickers)
        rows = self.journal.query(select(s.universe).where(
            s.universe.c.effective_date <= d, s.universe.c.on_owner_list.is_(True)))
        if not rows:
            return []
        latest = max(r["effective_date"] for r in rows)
        return [r["ticker"] for r in rows if r["effective_date"] == latest]

    def check_tradable(self, assets: dict[str, bool] | None, history_days: dict[str, int]
                       ) -> dict[str, str]:
        """UR-3: tradable on Alpaca with at least ``min_history_days`` of history.
        ``assets`` None: tradability is checked elsewhere (the live pre-flight), history only."""
        self.blocked = {}
        need = self.cfg.universe.min_history_days
        for t in self.cfg.universe.tickers:
            if assets is not None and not assets.get(t, False):
                self.blocked[t] = f"{t} not tradable on Alpaca (UR-3)"
            elif history_days.get(t, 0) < need:
                self.blocked[t] = f"{t} has {history_days.get(t, 0)} days of history < {need} " \
                                  "(UR-3)"
        for t, why in self.blocked.items():
            log.warning("ticker_blocked", ticker=t, reason=why)
        return self.blocked

    def weekly_compare(self, market_caps: dict[str, float], d: date,
                       vendor_shares: dict[str, float] | None = None,
                       edgar_shares: dict[str, float] | None = None) -> dict[str, Any]:
        """A1-2 / UR-4: current top 10 by market cap next to the owner's list. Never edits it.
        With both share counts, names whose Finnhub and SEC EDGAR shares outstanding differ by
        more than 10% are listed (the cap ranking may rest on a bad number)."""
        ranked = sorted(((t, c) for t, c in market_caps.items() if c), key=lambda x: -x[1])
        top10 = [t for t, _ in ranked[:10]]
        owner = list(self.cfg.universe.tickers)
        report: dict[str, Any] = {"date": d.isoformat(), "top10": top10, "owner_list": owner,
                  "not_in_top10": [t for t in owner if t not in top10],
                  "top10_not_on_list": [t for t in top10 if t not in owner],
                  "market_caps": {t: c for t, c in ranked[:15]}}
        vs, es = vendor_shares or {}, edgar_shares or {}
        check = {t: round(abs(vs[t] / es[t] - 1) * 100, 1) for t in vs if es.get(t, 0) > 0}
        report["share_check"] = check
        report["share_mismatch"] = sorted(t for t, pct in check.items() if pct > 10)
        if self.journal is not None:
            self.journal.insert_many(s.universe, [
                {"effective_date": d, "ticker": t, "rank": i + 1, "market_cap": c,
                 "on_owner_list": False} for i, (t, c) in enumerate(ranked[:10])])
        return report
