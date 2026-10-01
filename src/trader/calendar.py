"""NYSE sessions, macro events and trading windows (TW-1..TW-5, A10-1).

``MarketCalendar`` uses the offline ``exchange_calendars`` XNYS calendar (holidays and
half days); live trading cross-checks it with Alpaca's calendar at pre-flight.
``TradingWindows`` answers "may this style enter now?" and "when must it be flat?".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from trader.config import Config
from trader.timeutil import ET, et_datetime, parse_hhmm, to_et

Window = Literal["morning", "afternoon"]
Style = Literal["day", "swing"]


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime    # ET
    close: datetime   # ET
    early_close: bool


class MarketCalendar:
    """NYSE trading days from exchange_calendars (A10-1)."""

    def __init__(self, extra_closed: set[date] | None = None) -> None:
        self._extra_closed = extra_closed or set()

    @staticmethod
    @lru_cache(maxsize=1)
    def _xnys() -> Any:
        import exchange_calendars as xcals

        return xcals.get_calendar("XNYS", start="2015-01-01")

    def is_trading_day(self, d: date) -> bool:
        if d in self._extra_closed or d.weekday() >= 5:
            return False
        try:
            return bool(self._xnys().is_session(d.isoformat()))
        except Exception:   # noqa: BLE001 - outside calendar range: weekdays only
            return True

    def session(self, d: date) -> Session:
        if not self.is_trading_day(d):
            raise ValueError(f"{d} is not a trading day")
        try:
            cal = self._xnys()
            o = cal.session_open(d.isoformat()).to_pydatetime().astimezone(ET)
            c = cal.session_close(d.isoformat()).to_pydatetime().astimezone(ET)
        except Exception:   # noqa: BLE001 - outside calendar range
            o, c = et_datetime(d, time(9, 30)), et_datetime(d, time(16, 0))
        return Session(day=d, open=o, close=c, early_close=c.time() < time(16, 0))

    def next_trading_day(self, d: date) -> date:
        n = d + timedelta(days=1)
        while not self.is_trading_day(n):
            n += timedelta(days=1)
        return n

    def prev_trading_day(self, d: date) -> date:
        n = d - timedelta(days=1)
        while not self.is_trading_day(n):
            n -= timedelta(days=1)
        return n

    def trading_days_between(self, start: date, end: date) -> int:
        """Trading days after ``start`` up to and including ``end``."""
        count, d = 0, start
        while d < end:
            d += timedelta(days=1)
            if self.is_trading_day(d):
                count += 1
        return count

    def add_trading_days(self, d: date, n: int) -> date:
        out = d
        for _ in range(n):
            out = self.next_trading_day(out)
        return out


@dataclass(frozen=True)
class MacroCalendar:
    fomc: frozenset[date]
    cpi: frozenset[date]
    jobs: frozenset[date]
    extra_caution: frozenset[date]

    @classmethod
    def load(cls, path: str | Path) -> MacroCalendar:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

        def dates(key: str) -> frozenset[date]:
            section = raw.get(key) or {}
            items: list[Any] = []
            if isinstance(section, dict):
                for values in section.values():
                    items.extend(values or [])
            else:
                items.extend(section)
            return frozenset(date.fromisoformat(str(x)) for x in items)

        return cls(dates("fomc_decision_days"), dates("cpi_release_days"),
                   dates("jobs_report_days"), dates("extra_caution_days"))

    @classmethod
    def empty(cls) -> MacroCalendar:
        return cls(frozenset(), frozenset(), frozenset(), frozenset())

    def is_fomc(self, d: date) -> bool:
        return d in self.fomc

    def is_cpi_or_jobs(self, d: date) -> bool:
        return d in self.cpi or d in self.jobs or d in self.extra_caution

    def last_cpi(self) -> date | None:
        return max(self.cpi) if self.cpi else None

    def covers(self, d: date) -> bool:
        """A10 refuses to start if today is past the last listed CPI date."""
        last = self.last_cpi()
        return last is not None and d <= last

    def next_fomc(self, d: date) -> date | None:
        future = sorted(x for x in self.fomc if x >= d)
        return future[0] if future else None


class TradingWindows:
    """Entry and flatten rules per window (TW-1..TW-5)."""

    def __init__(self, cfg: Config, calendar: MarketCalendar, macro: MacroCalendar) -> None:
        self.cfg = cfg
        self.cal = calendar
        self.macro = macro
        w = cfg.windows
        self.m_observe = parse_hhmm(w.morning.observe_from)
        self.m_from = parse_hhmm(w.morning.entries_from)
        self.m_until = parse_hhmm(w.morning.entries_until)
        self.m_flatten = parse_hhmm(w.morning.flatten_at)
        self.macro_m_from = parse_hhmm(w.macro_days.cpi_jobs_morning_entries_from)
        self.a_from = parse_hhmm(w.afternoon.entries_from)
        self.a_day_until = parse_hhmm(w.afternoon.day_entries_until)
        self.a_swing_from = parse_hhmm(w.afternoon.swing_entries_from)
        self.a_swing_until = parse_hhmm(w.afternoon.swing_entries_until)
        self.a_flatten = parse_hhmm(w.afternoon.flatten_at)

    # -- day properties -------------------------------------------------------------------
    def morning_entries_from(self, d: date) -> time:
        """TW-5: CPI and jobs days start morning entries at 09:45."""
        return max(self.m_from, self.macro_m_from) if self.macro.is_cpi_or_jobs(d) else self.m_from

    def afternoon_open(self, d: date) -> bool:
        """TW-4 half days and TW-5 FOMC days have no afternoon window."""
        if not self.cal.is_trading_day(d):
            return False
        if self.cal.session(d).early_close:
            return False
        return not (self.cfg.windows.macro_days.skip_afternoon_on_fomc and self.macro.is_fomc(d))

    # -- queries ---------------------------------------------------------------------------
    def window_at(self, now: datetime) -> Window | None:
        """The management window containing ``now`` (9:30-10:30 or 15:00-close)."""
        local = to_et(now)
        d, t = local.date(), local.time()
        if not self.cal.is_trading_day(d):
            return None
        if self.m_observe <= t < self.m_flatten:
            return "morning"
        if self.afternoon_open(d) and self.a_from <= t < time(16, 0):
            return "afternoon"
        return None

    def entry_window(self, now: datetime, style: Style) -> tuple[Window | None, str]:
        """Return (window, "") if ``style`` may enter at ``now``, else (None, reason)."""
        local = to_et(now)
        d, t = local.date(), local.time()
        if not self.cal.is_trading_day(d):
            return None, "not a trading day"
        if style == "day":
            if self.morning_entries_from(d) <= t <= self.m_until:
                return "morning", ""
            if self.afternoon_open(d) and self.a_from <= t <= self.a_day_until:
                return "afternoon", ""
            return None, f"{t:%H:%M:%S} outside day-trade entry windows"
        # TW-3: swing entries only near the close in the afternoon window
        if self.afternoon_open(d) and self.a_swing_from <= t <= self.a_swing_until:
            return "afternoon", ""
        return None, f"{t:%H:%M:%S} outside swing entry window"

    def flatten_time(self, d: date, window: Window) -> datetime:
        """TW-2: morning day trades out by 10:30; all day trades by 15:55 (or before an early
        close)."""
        if window == "morning":
            return et_datetime(d, self.m_flatten)
        close = self.cal.session(d).close if self.cal.is_trading_day(d) else et_datetime(
            d, time(16, 0))
        flat = et_datetime(d, self.a_flatten)
        return min(flat, close - timedelta(minutes=5))

    def is_in_any_window(self, now: datetime) -> bool:
        return self.window_at(now) is not None
