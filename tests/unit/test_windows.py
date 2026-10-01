"""Trading windows (TW-1..TW-5) and the NYSE calendar (A10-1)."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import PLAIN_DAY, et
from trader.calendar import MarketCalendar, TradingWindows

CPI_DAY = date(2026, 10, 14)
FOMC_DAY = date(2026, 10, 28)
HALF_DAY = date(2026, 11, 27)     # day after Thanksgiving, 13:00 close
HOLIDAY = date(2026, 11, 26)      # Thanksgiving


@pytest.mark.rule("TW-1")
@pytest.mark.parametrize("hhmm,sec,ok", [("09:34", 59, False), ("09:35", 0, True),
                                         ("10:15", 0, True), ("10:15", 1, False),
                                         ("12:00", 0, False), ("15:00", 0, True),
                                         ("15:40", 0, True), ("15:40", 1, False)])
def test_day_entry_windows(windows: TradingWindows, hhmm: str, sec: int, ok: bool) -> None:
    w, _ = windows.entry_window(et(PLAIN_DAY, hhmm, sec), "day")
    assert (w is not None) == ok


@pytest.mark.rule("TW-1")
def test_no_entry_in_opening_noise(windows: TradingWindows) -> None:
    for m in range(30, 35):
        assert windows.entry_window(et(PLAIN_DAY, f"09:{m}"), "day")[0] is None


@pytest.mark.rule("TW-2")
@pytest.mark.rule("A9-2")
def test_flatten_times(windows: TradingWindows) -> None:
    assert windows.flatten_time(PLAIN_DAY, "morning") == et(PLAIN_DAY, "10:30")
    assert windows.flatten_time(PLAIN_DAY, "afternoon") == et(PLAIN_DAY, "15:55")


@pytest.mark.rule("TW-3")
@pytest.mark.parametrize("hhmm,ok", [("09:40", False), ("15:29", False), ("15:30", True),
                                     ("15:50", True), ("15:51", False)])
def test_swing_entries_only_near_close(windows: TradingWindows, hhmm: str, ok: bool) -> None:
    w, _ = windows.entry_window(et(PLAIN_DAY, hhmm), "swing")
    assert (w == "afternoon") == ok


@pytest.mark.rule("TW-4")
@pytest.mark.rule("A10-1")
def test_half_day_morning_only(windows: TradingWindows, cal: MarketCalendar) -> None:
    s = cal.session(HALF_DAY)
    assert s.early_close and s.close.hour == 13
    assert windows.entry_window(et(HALF_DAY, "09:40"), "day")[0] == "morning"
    assert not windows.afternoon_open(HALF_DAY)
    assert windows.entry_window(et(HALF_DAY, "15:10"), "day")[0] is None


@pytest.mark.rule("TW-5")
def test_cpi_day_morning_starts_0945(windows: TradingWindows) -> None:
    assert windows.entry_window(et(CPI_DAY, "09:40"), "day")[0] is None
    assert windows.entry_window(et(CPI_DAY, "09:45"), "day")[0] == "morning"


@pytest.mark.rule("TW-5")
def test_fomc_day_skips_afternoon(windows: TradingWindows) -> None:
    assert not windows.afternoon_open(FOMC_DAY)
    assert windows.entry_window(et(FOMC_DAY, "15:10"), "day")[0] is None
    assert windows.entry_window(et(FOMC_DAY, "15:35"), "swing")[0] is None
    assert windows.entry_window(et(FOMC_DAY, "09:40"), "day")[0] == "morning"


@pytest.mark.rule("A10-1")
def test_holidays_and_weekends_closed(windows: TradingWindows, cal: MarketCalendar) -> None:
    assert not cal.is_trading_day(HOLIDAY)
    assert not cal.is_trading_day(date(2026, 10, 17))   # Saturday
    assert windows.entry_window(et(HOLIDAY, "09:40"), "day")[0] is None
    assert windows.window_at(et(HOLIDAY, "09:40")) is None


def test_window_at(windows: TradingWindows) -> None:
    assert windows.window_at(et(PLAIN_DAY, "09:31")) == "morning"
    assert windows.window_at(et(PLAIN_DAY, "10:30")) is None
    assert windows.window_at(et(PLAIN_DAY, "15:00")) == "afternoon"
    assert windows.window_at(et(PLAIN_DAY, "12:00")) is None
