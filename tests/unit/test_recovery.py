"""Drawdown and recovery RC-1..RC-4."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import PLAIN_DAY, et
from tests.unit.test_loss_limits import trade
from trader.agents.a07_risk import RiskManager
from trader.agents.a12_compliance import ComplianceGuard

D = Decimal


@pytest.mark.rule("RC-1")
def test_risk_halves_3pct_below_hwm(risk: RiskManager) -> None:
    risk.state.hwm = D("26000")
    risk.mark(et(PLAIN_DAY, "10:00"), D("25220"), D("0"))   # exactly -3%
    assert risk.state.reduced_since == D("26000")
    assert risk.risk_pct(True) == D("0.25")


@pytest.mark.rule("RC-2")
def test_full_size_after_new_high(risk: RiskManager) -> None:
    risk.state.hwm = D("26000")
    risk.mark(et(PLAIN_DAY, "10:00"), D("25200"), D("0"))
    risk.mark(et(PLAIN_DAY, "11:00"), D("25900"), D("0"))   # recovered but no new high
    assert risk.risk_pct(True) == D("0.25")
    risk.mark(et(PLAIN_DAY, "12:00"), D("26001"), D("0"))
    assert risk.risk_pct(True) == D("0.5")


@pytest.mark.rule("RC-2")
def test_full_size_after_ten_good_reduced_trades(risk: RiskManager) -> None:
    risk.state.hwm = D("26000")
    risk.mark(et(PLAIN_DAY, "10:00"), D("25200"), D("0"))
    for i in range(10):
        risk.state.reduced_trades.append(0.3 if i % 2 else -0.1)
    risk.mark(et(PLAIN_DAY, "10:30"), D("25300"), D("0"))
    assert risk.state.reduced_since is None and risk.risk_pct(True) == D("0.5")


@pytest.mark.rule("RC-3")
def test_negative_setup_goes_back_to_paper(risk: RiskManager) -> None:
    for i in range(20):
        risk.state.history.append(trade("-10", -0.3 if i % 3 else 0.2, setup="VWAP_RECLAIM"))
    assert risk.setup_status("VWAP_RECLAIM") == "paper"
    assert risk.setup_status("ORB") == "live"


@pytest.mark.rule("RC-4")
def test_three_losses_lock_ticker_five_days(cfg, cal) -> None:  # type: ignore[no-untyped-def]
    cfg2 = cfg.model_copy(update={"wash_sale": cfg.wash_sale.model_copy(
        update={"policy": "year_end_only"})})
    g = ComplianceGuard(cfg2, cal)
    t = et(PLAIN_DAY, "10:00")
    for _ in range(3):
        locks = g.on_position_closed("TSLA", D("-30"), -1.0, t)
    assert any(lk.lock_type == "LOSS_STREAK" for lk in locks)
    res = g.check_entry("TSLA", et(PLAIN_DAY, "15:10"), "day")
    assert not res.ok and res.reason == "NT_LOSS_STREAK_LOCK"
    later = et(cal.add_trading_days(PLAIN_DAY, 5), "09:40")
    assert g.check_entry("TSLA", later, "day").ok
