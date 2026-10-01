"""Loss limits LL-1..LL-11, sizing A7-2 and the A7-3/A7-4 blocks."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import AGREE, PLAIN_DAY, et, make_ctx, make_setup
from trader.agents.a07_risk import ClosedTrade, OpenPositionView, RiskManager
from trader.agents.a08_execution import build_legs
from trader.models.messages import ComplianceResult

OK = ComplianceResult(proposal_id="p", ok=True)
D = Decimal


def trade(pnl: str, r: float, *, window: str = "morning", ticker: str = "AAPL",
          reason: str = "EXIT_STOP", setup: str = "ORB", reduced: bool = False) -> ClosedTrade:
    return ClosedTrade(position_id=f"x{pnl}{r}", ticker=ticker, setup=setup, style="day",
                       window=window, closed_at=et(PLAIN_DAY, "09:50"), pnl=D(pnl),
                       r_multiple=r, exit_reason=reason, reduced_size=reduced)


def pos(ticker: str, *, style: str = "day", entry: str = "100", last: str = "101",
        qty: int = 10, t1_hit: bool = False, stop: str = "99") -> OpenPositionView:
    return OpenPositionView(position_id=f"pos-{ticker}", ticker=ticker, group=ticker,
                            style=style, window="morning", qty=qty, entry=D(entry), stop=D(stop),
                            last=D(last), t1_hit=t1_hit)


@pytest.mark.rule("A7-2")
@pytest.mark.rule("LL-1")
@pytest.mark.rule("LL-9")
def test_worked_example_sizing(risk: RiskManager) -> None:
    """STRATEGY_SPEC §7: 25% cap binds -> 32 shares, legs 16/16."""
    qty, risk_usd, pct, binding = risk.size(D("190.49"), D("189.61"), D("25000"), D("25000"),
                                            D("0"), True)
    assert pct == D("0.5")
    assert qty == 32 and "LL-9" in binding
    assert risk_usd == D("28.16")
    legs = build_legs("pid", "NVDA", qty, D("190.49"), D("189.61"), D("191.37"), D("192.25"),
                      "day")
    assert [x.qty for x in legs] == [16, 16]


@pytest.mark.rule("LL-1")
def test_risk_bound_size(risk: RiskManager) -> None:
    qty, risk_usd, *_ = risk.size(D("50.00"), D("49.00"), D("100000"), D("100000"), D("0"),
                                  True)
    assert qty == 500 and risk_usd == D("500.00")   # 0.5% of 100k / $1 R


@pytest.mark.rule("LL-11")
@pytest.mark.rule("SR-2")
def test_leverage_never_above_1x(risk: RiskManager) -> None:
    qty, *_ , binding = risk.size(D("50.00"), D("49.90"), D("10000"), D("40000"), D("9000"),
                                  True)
    assert qty * D("50") <= D("10000") - D("9000") and "LL-11" in binding


@pytest.mark.rule("LL-2")
def test_max_trades_per_day_and_window(risk: RiskManager) -> None:
    risk.state.trades_today = {"morning": 2, "afternoon": 0}
    d = risk.evaluate(make_setup("AAPL"), make_ctx(), AGREE, OK, "p")
    assert d.reason_code == "HALT_LL2"
    risk.state.trades_today = {"morning": 1, "afternoon": 1}
    s = make_setup("AAPL", setup="POWER_HOUR_BREAKOUT", window="afternoon",
                   ts=et(PLAIN_DAY, "15:10"))
    d = risk.evaluate(s, make_ctx(et(PLAIN_DAY, "15:10", 5)), AGREE, OK, "p")
    assert d.reason_code == "HALT_LL2"


@pytest.mark.rule("LL-3")
@pytest.mark.rule("A7-4")
def test_daily_loss_limit_halts(risk: RiskManager) -> None:
    hits = risk.mark(et(PLAIN_DAY, "09:50"), D("24700"), D("-380"))   # -1.52% of 25k
    assert [h.rule for h in hits] == ["LL-3"] and risk.state.halted == "HALT_LL3"
    d = risk.evaluate(make_setup("AAPL"), make_ctx(), AGREE, OK, "p")
    assert d.reason_code == "HALT_LL3"


@pytest.mark.rule("LL-4")
def test_two_losses_in_a_row_halt(risk: RiskManager) -> None:
    assert not risk.on_trade_closed(trade("-50", -1.0, ticker="AAPL"), et(PLAIN_DAY, "09:50"))
    hits = risk.on_trade_closed(trade("-50", -1.0, ticker="MSFT"), et(PLAIN_DAY, "10:05"))
    assert "LL-4" in [h.rule for h in hits] and risk.state.halted == "HALT_LL4"


@pytest.mark.rule("LL-4")
def test_win_resets_the_streak(risk: RiskManager) -> None:
    risk.on_trade_closed(trade("-50", -1.0), et(PLAIN_DAY, "09:50"))
    risk.on_trade_closed(trade("80", 1.6, ticker="MSFT"), et(PLAIN_DAY, "10:00"))
    assert not risk.on_trade_closed(trade("-50", -1.0, ticker="AMZN", window="afternoon"),
                                    et(PLAIN_DAY, "15:20"))


@pytest.mark.rule("LL-5")
def test_weekly_limit_forces_paper(cfg, windows) -> None:  # type: ignore[no-untyped-def]
    live = cfg.model_copy(update={"mode": cfg.mode.model_copy(update={"environment": "live"})})
    r = RiskManager(live, windows)
    r.start_day(PLAIN_DAY, D("25000"))
    r.state.week_start_equity = D("25000")
    hits = r.mark(et(PLAIN_DAY, "10:00"), D("24200"), D("0"))   # -3.2%
    assert "LL-5" in [h.rule for h in hits]
    assert r.state.paper_only_until is not None and r.state.paper_only_until.weekday() == 0
    assert r.route_for("ORB", "day") == "paper"


@pytest.mark.rule("LL-6")
def test_monthly_drawdown_stops_live(cfg, windows) -> None:  # type: ignore[no-untyped-def]
    live = cfg.model_copy(update={"mode": cfg.mode.model_copy(update={"environment": "live"})})
    r = RiskManager(live, windows)
    r.start_day(PLAIN_DAY, D("25000"))
    r.state.month_peak = D("26000")
    r.state.week_start_equity = D("24000")
    hits = r.mark(et(PLAIN_DAY, "10:00"), D("24400"), D("0"))   # -6.15% from the month peak
    assert "LL-6" in [h.rule for h in hits] and r.state.live_disabled
    assert r.route_for("ORB", "day") == "paper"


@pytest.mark.rule("LL-7")
def test_profit_giveback_halts(risk: RiskManager) -> None:
    # 1R = 0.5% x 25k = 125. Peak +150, then back to +70 (gave back > 50%)
    risk.on_trade_closed(trade("150", 1.2, ticker="MSFT"), et(PLAIN_DAY, "09:50"))
    hits = risk.mark(et(PLAIN_DAY, "10:00"), D("25070"), D("-80"))
    assert "LL-7" in [h.rule for h in hits] and risk.state.halted == "HALT_LL7"


@pytest.mark.rule("LL-7")
def test_giveback_needs_at_least_1r(risk: RiskManager) -> None:
    risk.on_trade_closed(trade("100", 0.8, ticker="MSFT"), et(PLAIN_DAY, "09:50"))
    assert not risk.mark(et(PLAIN_DAY, "10:00"), D("25020"), D("-80"))


@pytest.mark.rule("LL-8")
def test_max_open_positions(risk: RiskManager) -> None:
    ps = [pos("AAPL"), pos("MSFT"), pos("AMZN", style="swing")]
    d = risk.evaluate(make_setup("META", trigger="700", entry="700.10", stop="699.40",
                                 atr5=1.0, resistance=None),
                      make_ctx(positions=ps, last="700.05", equity="100000", bp="100000"),
                      AGREE, OK, "p")
    assert d.reason_code == "HALT_LL8"


@pytest.mark.rule("LL-8")
def test_max_two_day_trades(risk: RiskManager) -> None:
    ps = [pos("AAPL"), pos("MSFT")]
    d = risk.evaluate(make_setup("META", trigger="700", entry="700.10", stop="699.40",
                                 atr5=1.0, resistance=None),
                      make_ctx(positions=ps, last="700.05", equity="100000", bp="100000"),
                      AGREE, OK, "p")
    assert d.reason_code == "HALT_LL8"


@pytest.mark.rule("LL-10")
def test_correlated_semis_max_one(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup("NVDA"), make_ctx(positions=[pos("AVGO")]), AGREE, OK, "p")
    assert d.reason_code == "HALT_LL10"


@pytest.mark.rule("A7-3")
def test_limits_read_only(risk: RiskManager) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        risk.cfg.risk.max_trades_per_day = 99   # type: ignore[misc]


@pytest.mark.rule("A7-4")
def test_blocks_on_stale_data_kill_and_health(risk: RiskManager) -> None:
    assert risk.evaluate(make_setup(), make_ctx(data_stale=True), AGREE, OK, "p"
                         ).reason_code == "NT_FILTER_MF5"
    assert risk.evaluate(make_setup(), make_ctx(health_ok=False, health_detail="ollama down"),
                         AGREE, OK, "p").reason_code == "NT_HEALTH"
    risk.state.kill_switch = True
    assert risk.evaluate(make_setup(), make_ctx(), AGREE, OK, "p").reason_code == \
        "NT_KILL_SWITCH"


def test_new_day_resets_daily_limits(risk: RiskManager) -> None:
    risk.state.halted = "HALT_LL4"
    risk.start_day(PLAIN_DAY + timedelta(days=3), D("25000"))
    assert risk.state.halted is None and risk.state.trades_total_today == 0
