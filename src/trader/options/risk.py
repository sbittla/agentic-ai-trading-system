"""A15 Options Risk & Compliance (code): A7 and A12 extended to options.

- A15-1 wash sales both ways: a locked ticker locks its options; an option loss locks the
  stock (and its options) for 31 days. Strict policy: every contract is "identical".
- A15-2 shared limits: daily/weekly/monthly loss limits and the trade budget; each
  suggestion counts as one trade with its window caps; never a stock entry and an options
  suggestion on the same ticker and direction in one window.
- A15-3 events: no long premium through earnings (OP-2, in A14); no short call through an
  ex-dividend date; nothing in the last 15 minutes; FOMC days: day ideas only before 13:30.
- A15-4 account level: long options need level 2, spreads level 3 (in A14 too).
- A15-5 correlated groups: one open per group, options included.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from trader.options.strategist import LEVEL_NEEDED, OptionSuggestion
from trader.timeutil import to_et


@dataclass
class OptionsRiskState:
    window_counts: dict[tuple[date, str], int] = field(default_factory=dict)
    per_ticker: set[tuple[date, str, str, str]] = field(default_factory=set)  # OP-15
    open_tickers: dict[str, str] = field(default_factory=dict)   # suggestion id -> ticker


class OptionsRisk:
    def __init__(self, cfg: Any, risk: Any, compliance: Any, macro: Any = None,
                 stock_entries: Any = None) -> None:
        """``risk``: the A7 RiskManager (shared state); ``compliance``: A12;
        ``stock_entries(ticker, window) -> set of directions`` entered today (A15-2)."""
        self.cfg = cfg
        self.risk = risk
        self.compliance = compliance
        self.macro = macro
        self.stock_entries: Callable[[str, str], set[str]] = stock_entries or (
            lambda _t, _w: set())
        self.state = OptionsRiskState()

    def group_of(self, ticker: str) -> str | None:
        for name, g in self.cfg.universe.correlated_groups.items():
            if ticker in g.tickers:
                return str(name)
        return None

    def check(self, s_: OptionSuggestion, now: datetime, ex_div: date | None = None
              ) -> tuple[bool, str]:
        v, ocfg = s_.view, self.cfg.options
        st = self.risk.state if self.risk is not None else None
        t = to_et(now).time()
        d = to_et(now).date()
        window = v.window or ("morning" if t < time(12, 0) else "afternoon")
        # A15-1 wash-sale locks (strict: options on a locked ticker are locked)
        if self.compliance is not None and self.compliance.active_locks(now, v.ticker):
            lk = self.compliance.active_locks(now, v.ticker)[0]
            return False, f"A15-1 {v.ticker} locked ({lk.lock_type}) until {lk.until:%Y-%m-%d}"
        # A15-2 shared limits
        if st is not None:
            if st.kill_switch:
                return False, "A15-2 kill switch on"
            if st.halted:
                return False, f"A15-2 trading halted today ({st.halted})"
            if st.paper_only_until is not None and ocfg.mode == "live":
                return False, "A15-2 weekly loss limit: paper only"
            if st.live_disabled and ocfg.mode == "live":
                return False, "A15-2 monthly drawdown: live off"
            if st.trades_total_today >= self.cfg.risk.max_trades_per_day:
                return False, f"A15-2 trade budget used ({st.trades_total_today})"
            cap = (self.cfg.risk.max_trades_morning if window == "morning"
                   else self.cfg.risk.max_trades_afternoon)
            if st.trades_today.get(window, 0) >= cap:
                return False, f"A15-2 {window} trade cap reached"
        direction = "long" if v.direction == "bull" else "short"
        if direction in self.stock_entries(v.ticker, window):
            return False, f"A15-2 stock entry on {v.ticker} in the {window} window already"
        # OP-15 one per ticker per direction per window; max per window
        if (d, window, v.ticker, v.direction) in self.state.per_ticker:
            return False, f"OP-15 already suggested {v.ticker} {v.direction} this window"
        if self.state.window_counts.get((d, window), 0) >= ocfg.max_alerts_per_window:
            return False, f"OP-15 {ocfg.max_alerts_per_window} options alerts this window"
        # A15-3 events
        if t >= time(15, 45) and v.source != "idea":
            return False, "A15-3 no new suggestions in the last 15 minutes"
        if (self.macro is not None and v.style == "day" and t >= time(13, 30)
                and self.macro.is_fomc(d)):
            return False, "A15-3 FOMC day: day ideas only before 13:30"
        if ex_div is not None and s_.structure == "call_credit" and d <= ex_div <= s_.expiry:
            return False, "A15-3 short call through an ex-dividend date (early assignment)"
        # A15-4 account level
        if LEVEL_NEEDED[s_.structure] > ocfg.account_level:
            return False, f"A15-4 {s_.structure} needs level {LEVEL_NEEDED[s_.structure]}"
        # A15-5 correlated groups (stock positions and open suggestions)
        g = self.group_of(v.ticker)
        if g is not None:
            grp = self.cfg.universe.correlated_groups[g]
            held = sum(1 for x in self.state.open_tickers.values() if x in grp.tickers
                       and x != v.ticker)
            if held >= grp.max_open:
                return False, f"A15-5 {g} group already has {held} open"
        return True, ""

    def record(self, s_: OptionSuggestion, now: datetime) -> None:
        """A sent suggestion counts as one trade (A15-2) and one OP-15 slot."""
        d = to_et(now).date()
        window = s_.view.window or ("morning" if to_et(now).time() < time(12, 0)
                                    else "afternoon")
        self.state.per_ticker.add((d, window, s_.view.ticker, s_.view.direction))
        self.state.window_counts[(d, window)] = self.state.window_counts.get((d, window), 0) + 1
        self.state.open_tickers[s_.suggestion_id] = s_.view.ticker
        if self.risk is not None and s_.view.source != "idea":
            st = self.risk.state
            st.trades_today[window] = st.trades_today.get(window, 0) + 1

    def closed(self, s_: OptionSuggestion, pnl: float, now: datetime) -> None:
        """A15-1: an option loss locks the stock and its options for ``lock_days``."""
        self.state.open_tickers.pop(s_.suggestion_id, None)
        if pnl < 0 and self.compliance is not None:
            days = self.cfg.wash_sale.lock_days
            self.compliance.add_lock(s_.view.ticker, "WASH", now, now + timedelta(days=days),
                                     f"option loss {pnl:.2f} on {s_.structure} (A15-1)")
