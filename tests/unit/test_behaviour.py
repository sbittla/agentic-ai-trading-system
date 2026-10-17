"""Behaviour rules DR-1..DR-8."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import AGREE, PLAIN_DAY, et, make_ctx, make_setup
from tests.unit.test_loss_limits import pos, trade
from trader.agents.a07_risk import RiskManager
from trader.agents.a09_positions import PositionMonitor, StopMove, StopWidenError
from trader.models.messages import ComplianceResult

OK = ComplianceResult(proposal_id="p", ok=True)
D = Decimal
S = dict(trigger="100.00", entry="100.10", stop="99.40", atr5=1.0, resistance=None)


@pytest.mark.rule("DR-1")
def test_never_average_down(risk: RiskManager) -> None:
    losing = pos("AAPL", entry="101", last="100.5")
    d = risk.evaluate(make_setup("AAPL", **S), make_ctx(positions=[losing], last="100.05"),
                      AGREE, OK, "p")
    assert d.reason_code == "NT_DR1"


@pytest.mark.rule("DR-2")
@pytest.mark.rule("A9-4")
def test_stop_never_widened(cfg, windows, cal) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.test_exits import open_position

    pm = PositionMonitor(cfg, windows, cal)
    p = open_position(pm)
    with pytest.raises(StopWidenError):
        pm.apply_stop_move(StopMove(p.position_id, p.stop - D("0.10"), "XR-3 trail"))
    pm.apply_stop_move(StopMove(p.position_id, p.stop + D("0.10"), "XR-3 trail"))


@pytest.mark.rule("DR-3")
def test_ticker_locked_after_loss_in_window(risk: RiskManager) -> None:
    risk.on_trade_closed(trade("-40", -1.0, ticker="AAPL", reason="EXIT_FAILED_TRADE"),
                         et(PLAIN_DAY, "09:50"))
    d = risk.evaluate(make_setup("AAPL", **S, ts=et(PLAIN_DAY, "10:05")),
                      make_ctx(et(PLAIN_DAY, "10:05", 5), last="100.05"), AGREE, OK, "p")
    assert d.reason_code == "NT_DR3"
    # afternoon window: the lock is per window
    s = make_setup("AAPL", setup="POWER_HOUR_BREAKOUT", window="afternoon",
                   ts=et(PLAIN_DAY, "15:10"), **S)
    assert risk.evaluate(s, make_ctx(et(PLAIN_DAY, "15:10", 5), last="100.05"), AGREE, OK,
                         "p2").result == "APPROVE"


@pytest.mark.rule("DR-3")
def test_ten_minute_cooldown_after_stop(risk: RiskManager) -> None:
    risk.on_trade_closed(trade("-40", -1.0, ticker="AAPL", reason="EXIT_STOP"),
                         et(PLAIN_DAY, "09:50"))
    s = make_setup("MSFT", ts=et(PLAIN_DAY, "09:55"), **S)
    d = risk.evaluate(s, make_ctx(et(PLAIN_DAY, "09:55", 5), last="100.05"), AGREE, OK, "p")
    assert d.reason_code == "NT_DR3"
    s = make_setup("MSFT", ts=et(PLAIN_DAY, "10:01"), **S)
    assert risk.evaluate(s, make_ctx(et(PLAIN_DAY, "10:01", 5), last="100.05"), AGREE, OK,
                         "p").result == "APPROVE"


@pytest.mark.rule("DR-4")
def test_one_trade_per_ticker_per_window(risk: RiskManager) -> None:
    risk.on_entry_submitted(make_setup("AAPL", **S))
    d = risk.evaluate(make_setup("AAPL", **S, ts=et(PLAIN_DAY, "10:00")),
                      make_ctx(et(PLAIN_DAY, "10:00", 5), last="100.05"), AGREE, OK, "p")
    assert d.reason_code == "NT_DR4"


@pytest.mark.rule("DR-5")
def test_add_only_to_winners_after_t1(risk: RiskManager) -> None:
    winner_no_t1 = pos("AAPL", entry="99", last="100.05")
    d = risk.evaluate(make_setup("AAPL", **S), make_ctx(positions=[winner_no_t1],
                                                        last="100.05"), AGREE, OK, "p")
    assert d.reason_code == "NT_DR5"
    winner = pos("AAPL", entry="99", last="100.05", t1_hit=True, stop="99")
    d = risk.evaluate(make_setup("AAPL", **S), make_ctx(positions=[winner], last="100.05"),
                      AGREE, OK, "p")
    assert d.result == "APPROVE"


@pytest.mark.rule("DR-6")
def test_day_trade_never_becomes_swing(cfg, windows, cal) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.test_exits import open_position

    pm = PositionMonitor(cfg, windows, cal)
    open_position(pm)
    # no conversion path exists: a day trade still open on a later day is closed at the
    # first tick
    acts = pm.on_tick(et(PLAIN_DAY, "09:31") + timedelta(days=3))
    assert acts and acts[0].reason == "EXIT_TIME_STOP_1555"


@pytest.mark.rule("DR-7")
def test_unplanned_half_risk(risk: RiskManager) -> None:
    assert risk.risk_pct(planned=False) == risk.risk_pct(planned=True) / 2


@pytest.mark.rule("DR-8")
def test_owner_override_is_tagged(cfg, windows) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import DISAGREE

    c = cfg.model_copy(update={"mode": cfg.mode.model_copy(update={"day_trades": "approve"})})
    r = RiskManager(c, windows)
    r.start_day(PLAIN_DAY, D("25000"))
    d = r.evaluate(make_setup(), make_ctx(), DISAGREE, OK, "p")
    assert d.override is True   # if the owner approves it anyway, it is reported separately
