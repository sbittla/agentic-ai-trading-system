"""Exit rules XR-1..XR-6 and the position monitor A9-1..A9-4."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.conftest import PLAIN_DAY, bar, et
from trader.agents.a08_execution import EntryTicket
from trader.agents.a09_positions import ExitAction, ManagedPosition, PositionMonitor, StopMove
from trader.models.messages import EventFlags, Levels

D = Decimal


def ticket(style: str = "day", window: str = "morning", *, entry: str = "100.10",
           stop: str = "99.40", t1: str = "100.80", t2: str = "101.50") -> EntryTicket:
    return EntryTicket(proposal_id=f"pid-{style}", ticker="AAPL", style=style, window=window,
                       setup="ORB" if style == "day" else "SWING_BASE_BREAKOUT", planned=True,
                       trigger=D("100.00"), atr5=D("1.0"), entry=D(entry), stop=D(stop),
                       t1=D(t1), t2=D(t2), qty=20, route="broker",
                       submitted_at=et(PLAIN_DAY, "09:45"), breakout_level=100.0)


def open_position(pm: PositionMonitor, style: str = "day", window: str = "morning",
                  at: str = "09:46", d: date = PLAIN_DAY) -> ManagedPosition:
    t = ticket(style, window)
    pm.on_entry_fill(t, "A", "ordA", 10, D("100.10"), et(d, at))
    p, _ = pm.on_entry_fill(t, "B", "ordB", 10, D("100.10"), et(d, at))
    pm.register_child("ordA", "tpA", "take_profit")
    pm.register_child("ordA", "slA", "stop_loss")
    pm.register_child("ordB", "tpB", "take_profit")
    pm.register_child("ordB", "slB", "stop_loss")
    return p


@pytest.fixture
def pm(cfg, windows, cal) -> PositionMonitor:  # type: ignore[no-untyped-def]
    return PositionMonitor(cfg, windows, cal)


@pytest.mark.rule("XR-1")
@pytest.mark.rule("A9-1")
def test_hard_stop_fill_closes_with_stop_reason(pm: PositionMonitor) -> None:
    p = open_position(pm)
    pm.on_exit_fill("slA", 10, D("99.40"), et(PLAIN_DAY, "09:52"))
    p2, event, _ = pm.on_exit_fill("slB", 10, D("99.40"), et(PLAIN_DAY, "09:52"))
    assert p2 is p and event == "EXIT_STOP" and p.closed
    assert p.r_multiple() == pytest.approx(-1.0)


@pytest.mark.rule("XR-2")
def test_t1_moves_rest_to_break_even(pm: PositionMonitor) -> None:
    p = open_position(pm)
    _, event, acts = pm.on_exit_fill("tpA", 10, D("100.80"), et(PLAIN_DAY, "09:55"))
    assert event == "EXIT_T1" and p.t1_hit
    assert acts == [StopMove(p.position_id, D("100.10"), "XR-2 break-even after T1")]
    pm.apply_stop_move(acts[0])
    _, event, _ = pm.on_exit_fill("slB", 10, D("100.10"), et(PLAIN_DAY, "10:05"))
    assert event == "EXIT_BREAKEVEN" and p.closed
    assert p.r_multiple() == pytest.approx(0.5)


@pytest.mark.rule("XR-2")
def test_swing_t1_does_not_force_break_even(pm: PositionMonitor) -> None:
    p = open_position(pm, "swing", "afternoon", "15:46")
    _, event, acts = pm.on_exit_fill("tpA", 10, D("100.80"), et(PLAIN_DAY, "15:58"))
    assert event == "EXIT_T1" and acts == [] and p.style == "swing"


@pytest.mark.rule("XR-3")
def test_trail_under_ema9_after_t1(pm: PositionMonitor) -> None:
    p = open_position(pm)
    pm.on_exit_fill("tpA", 10, D("100.80"), et(PLAIN_DAY, "09:55"))
    pm.apply_stop_move(StopMove(p.position_id, D("100.10"), "XR-2 break-even after T1"))
    b5 = bar("AAPL", et(PLAIN_DAY, "10:00"), 101.0, 101.4, 100.9, 101.3, tf="5Min")
    acts = pm.on_5m(b5, et(PLAIN_DAY, "10:05"), vwap=100.2, ema9=100.95, atr5=1.0,
                    levels=None, last5=[b5])
    assert acts == [StopMove(p.position_id, D("100.90"), "XR-3 trail")]


@pytest.mark.rule("XR-3")
def test_no_trail_before_t1(pm: PositionMonitor) -> None:
    open_position(pm)
    b5 = bar("AAPL", et(PLAIN_DAY, "09:45"), 100.2, 100.6, 100.15, 100.5, tf="5Min")
    assert pm.on_5m(b5, et(PLAIN_DAY, "09:50"), vwap=100.0, ema9=100.3, atr5=1.0, levels=None,
                    last5=[b5]) == []


@pytest.mark.rule("XR-3")
def test_swing_trail_10_day_ema(pm: PositionMonitor) -> None:
    p = open_position(pm, "swing", "afternoon", "15:46")
    nxt = PLAIN_DAY + timedelta(days=3)
    lv = Levels(day=nxt, ticker="AAPL", prior_high=103, prior_low=101, prior_close=102.5,
                ema10d=101.2)
    acts = pm.start_day(nxt, {"AAPL": lv})
    assert acts == [StopMove(p.position_id, D("101.20"), "XR-3 swing trail 10-day EMA")]


@pytest.mark.rule("XR-4")
def test_failed_trade_exit_below_vwap(pm: PositionMonitor) -> None:
    open_position(pm)
    b5 = bar("AAPL", et(PLAIN_DAY, "09:45"), 100.2, 100.3, 99.8, 99.9, tf="5Min")
    acts = pm.on_5m(b5, et(PLAIN_DAY, "09:50"), vwap=100.05, ema9=100.1, atr5=1.0, levels=None,
                    last5=[b5])
    assert len(acts) == 1 and isinstance(acts[0], ExitAction)
    assert acts[0].reason == "EXIT_FAILED_TRADE"


@pytest.mark.rule("XR-4")
def test_swing_failed_on_close_below_ema20(pm: PositionMonitor) -> None:
    open_position(pm, "swing", "afternoon", "15:46")
    nxt = PLAIN_DAY + timedelta(days=3)
    lv = Levels(day=nxt, ticker="AAPL", prior_high=103, prior_low=101, prior_close=102.5,
                ema20d=100.5)
    b5 = bar("AAPL", et(nxt, "15:45"), 100.6, 100.6, 100.2, 100.3, tf="5Min")
    acts = pm.on_5m(b5, et(nxt, "15:50"), vwap=100.4, ema9=100.4, atr5=1.0, levels=lv,
                    last5=[b5])
    assert [a.reason for a in acts] == ["EXIT_FAILED_TRADE"]


@pytest.mark.rule("XR-5")
def test_time_stop_15_minutes_without_progress(pm: PositionMonitor) -> None:
    open_position(pm)   # entry 100.10, R 0.70 -> needs 100.45 within 15 min
    b = bar("AAPL", et(PLAIN_DAY, "10:00"), 100.2, 100.4, 100.1, 100.3)
    assert pm.on_1m(b, et(PLAIN_DAY, "10:00")) == []
    acts = pm.on_1m(b, et(PLAIN_DAY, "10:01"))
    assert [a.reason for a in acts] == ["EXIT_TIME_STOP_15MIN"]


@pytest.mark.rule("XR-5")
@pytest.mark.rule("A9-2")
@pytest.mark.rule("TW-2")
def test_hard_exits_1030_and_1555(pm: PositionMonitor) -> None:
    p = open_position(pm)
    p.max_high = 101.0   # made progress, so no 15-minute stop
    assert pm.on_tick(et(PLAIN_DAY, "10:29", 59)) == []
    assert [a.reason for a in pm.on_tick(et(PLAIN_DAY, "10:30"))] == ["EXIT_TIME_STOP_1030"]
    pm2 = PositionMonitor(pm.cfg, pm.windows, pm.cal)
    p2 = open_position(pm2, window="afternoon", at="15:10")
    p2.max_high = 101.0
    assert pm2.on_tick(et(PLAIN_DAY, "15:54")) == []
    assert [a.reason for a in pm2.on_tick(et(PLAIN_DAY, "15:55"))] == ["EXIT_TIME_STOP_1555"]


@pytest.mark.rule("XR-5")
@pytest.mark.rule("A9-3")
def test_swing_exits_after_10_trading_days(pm: PositionMonitor, cal) -> None:  # type: ignore[no-untyped-def]
    open_position(pm, "swing", "afternoon", "15:46")
    d10 = cal.add_trading_days(PLAIN_DAY, 10)
    b5 = bar("AAPL", et(d10, "15:45"), 102, 102.2, 101.9, 102.1, tf="5Min")
    acts = pm.on_5m(b5, et(d10, "15:50"), vwap=102, ema9=102, atr5=1.0, levels=None,
                    last5=[b5])
    assert [a.reason for a in acts] == ["EXIT_SWING_MAX_HOLD"]


@pytest.mark.rule("XR-6")
@pytest.mark.rule("A9-3")
def test_swing_closed_before_earnings(pm: PositionMonitor, cal) -> None:  # type: ignore[no-untyped-def]
    open_position(pm, "swing", "afternoon", "15:46")
    d = cal.add_trading_days(PLAIN_DAY, 2)
    pm.news["AAPL"] = EventFlags(ticker="AAPL", updated_at=et(d, "07:15"),
                                 flags=("EARNINGS_3D",),
                                 earnings_date=cal.next_trading_day(d))
    b5 = bar("AAPL", et(d, "15:45"), 102, 102.2, 101.9, 102.1, tf="5Min")
    acts = pm.on_5m(b5, et(d, "15:50"), vwap=102, ema9=102, atr5=1.0, levels=None, last5=[b5])
    assert [a.reason for a in acts] == ["EXIT_EARNINGS"]


@pytest.mark.rule("A9-4")
def test_never_adds_to_loser(pm: PositionMonitor, risk) -> None:  # type: ignore[no-untyped-def]
    """A9 never issues buys; adding is only possible through A7, which blocks losers."""
    p = open_position(pm)
    acts = pm.on_1m(bar("AAPL", et(PLAIN_DAY, "09:50"), 99.8, 99.9, 99.6, 99.7),
                    et(PLAIN_DAY, "09:50"))
    assert all(not isinstance(a, StopMove) or a.new_stop >= p.stop for a in acts)
