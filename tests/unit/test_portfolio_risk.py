"""Portfolio risk PF-1..PF-4 and pre-trade order sanity OC-1..OC-4 (GAP_REVIEW.md)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.conftest import AGREE, PLAIN_DAY, bar, et, make_ctx, make_setup
from trader.agents.a07_risk import OpenPositionView, RiskManager
from trader.agents.a08_execution import order_sanity
from trader.config import Config
from trader.models.messages import ComplianceResult
from trader.models.reasons import Reason
from trader.portfolio import PortfolioStats, compute, percentile
from trader.timeutil import ET

OK = ComplianceResult(proposal_id="p", ok=True)
D = Decimal


def pos(ticker: str, *, entry: str = "100", last: str = "100", stop: str = "98",
        qty: int = 10, style: str = "swing") -> OpenPositionView:
    return OpenPositionView(position_id=f"pos-{ticker}", ticker=ticker, group=ticker,
                            style=style, window="afternoon", qty=qty, entry=D(entry),
                            stop=D(stop), last=D(last))


def daily(symbol: str, closes: list[float], opens: list[float] | None = None,
          start: date = date(2025, 1, 2), v: int = 1_000_000) -> list:
    out = []
    d = datetime(start.year, start.month, start.day, 16, 0, tzinfo=ET)
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        out.append(bar(symbol, d + timedelta(days=i), o, max(o, c), min(o, c), c, v, "1Day"))
    return out


@pytest.mark.rule("PF-2")
def test_correlation_and_beta_from_daily_bars() -> None:
    base = [100 * (1 + 0.01 * ((i * 7) % 5 - 2)) for i in range(80)]
    spy = daily("SPY", base)
    twin = daily("AAA", [x * 2 for x in base])                   # same returns: corr 1, beta 1
    inverse = daily("BBB", [300 - x for x in base])               # moves the other way
    st = compute({"AAA": twin, "BBB": inverse}, spy, None)
    assert st.correlation("AAA", "BBB") is not None and st.correlation("AAA", "BBB") < -0.9
    assert st.beta["AAA"] == pytest.approx(1.0, abs=1e-6)
    assert st.beta_of("ZZZ") == 1.0                               # unknown: market-like


@pytest.mark.rule("PF-2")
def test_highly_correlated_name_is_refused(cfg: Config, risk: RiskManager) -> None:
    risk.state.portfolio = PortfolioStats(corr={frozenset(("AAPL", "MSFT")): 0.86})
    s = make_setup("AAPL", setup="ORB")
    d = risk.evaluate(s, make_ctx(positions=[pos("MSFT", style="day")]), AGREE, OK, "p1")
    assert d.result == "REJECT" and d.reason_code == Reason.NT_PF_CORRELATION
    risk.state.portfolio = PortfolioStats(corr={frozenset(("AAPL", "MSFT")): 0.55})
    d2 = risk.evaluate(s, make_ctx(positions=[pos("MSFT", style="day")]), AGREE, OK, "p2")
    assert d2.reason_code != Reason.NT_PF_CORRELATION


@pytest.mark.rule("PF-1")
def test_open_risk_cap_shrinks_the_new_trade(risk: RiskManager) -> None:
    # equity 25,000 -> cap 1.5% = 375. Open: 100 x (100 - 98) + 50 x 2 = 300 -> room 75.
    book = [pos("AAPL", qty=100), pos("MSFT", qty=50)]
    qty, _, _, binding = risk.size(D("50"), D("49"), D("25000"), D("25000"), D("0"), True,
                                   ticker="AMZN", style="day", positions=book)
    assert binding == "PF-1 open risk" and qty == 75
    # a stop locked above the last price adds no open risk
    locked = [pos("AAPL", qty=100, stop="101", last="100.5")]
    q2, *_ = risk.size(D("50"), D("49"), D("25000"), D("25000"), D("0"), True,
                       ticker="AMZN", style="day", positions=locked)
    assert q2 == 125          # back to the 0.5% risk size


@pytest.mark.rule("PF-3")
def test_beta_weighted_exposure_cap(risk: RiskManager) -> None:
    risk.state.portfolio = PortfolioStats(beta={"TSLA": 2.0, "NVDA": 2.0})
    # 150% of 100,000 = 150,000 beta-dollars; NVDA 300 x 200 x 2.0 = 120,000 used
    book = [pos("NVDA", entry="200", last="200", stop="199.99", qty=300)]
    qty, _, _, binding = risk.size(D("100"), D("99.9"), D("100000"), D("100000"), D("0"), True,
                                   ticker="TSLA", style="day", positions=book)
    assert binding == "PF-3 beta exposure" and qty == 150    # 30,000 / (2.0 x 100)


@pytest.mark.rule("PF-4")
def test_swing_size_capped_by_overnight_gap(risk: RiskManager) -> None:
    risk.state.portfolio = PortfolioStats(gap={"NVDA": 0.06})
    # stressed loss per share = 2 + 6% x 100 = 8; 1.5% of 25,000 = 375 -> 46 shares
    sw = risk.size(D("100"), D("98"), D("25000"), D("25000"), D("0"), True,
                   ticker="NVDA", style="swing", positions=[])
    assert sw[0] == 46 and sw[3] == "PF-4 gap risk"
    day = risk.size(D("100"), D("98"), D("25000"), D("25000"), D("0"), True,
                    ticker="NVDA", style="day", positions=[])
    assert day[0] == 62 and day[3] != "PF-4 gap risk"   # day trades are flat overnight


@pytest.mark.rule("PF-4")
def test_gap_percentile_uses_adverse_gaps_only() -> None:
    closes = [100.0] * 60
    opens = [100.0] * 60
    opens[30] = 90.0          # one 10% gap down
    opens[40] = 110.0         # gap up: not adverse
    st = compute({"X": daily("X", closes, opens)}, None, None, gap_pct=99)
    assert 0.03 < st.gap["X"] <= 0.10
    flat = compute({"X": daily("X", closes, [100.0] * 30 + [110.0] * 30)}, None, None)
    assert flat.gap["X"] == 0.0
    assert percentile([1, 2, 3, 4], 50) == 2.5


@pytest.mark.rule("OC-1")
@pytest.mark.rule("OC-3")
def test_order_sanity_price_collar_and_shape(cfg: Config) -> None:
    ok = order_sanity(cfg, "NVDA", 10, D("190.49"), D("189.61"), D("191.37"), D("192.25"),
                      D("190.45"), 5e6)
    assert ok is None
    far = order_sanity(cfg, "NVDA", 10, D("199.00"), D("189.61"), D("200"), D("201"),
                       D("190.45"), 5e6)
    assert far is not None and far.startswith("OC-1")
    bad = order_sanity(cfg, "NVDA", 10, D("190"), D("191"), D("192"), D("193"), None, None)
    assert bad is not None and bad.startswith("OC-3")


@pytest.mark.rule("OC-2")
@pytest.mark.rule("OC-4")
def test_order_sanity_volume_and_notional(cfg: Config) -> None:
    big = order_sanity(cfg, "XYZ", 20_000, D("10"), D("9.9"), D("10.2"), D("10.4"), D("10"),
                       1_000_000)
    assert big is not None and big.startswith("OC-2")
    capped = cfg.model_copy(update={"execution": cfg.execution.model_copy(
        update={"max_order_notional": 1000.0})})
    n = order_sanity(capped, "XYZ", 200, D("10"), D("9.9"), D("10.2"), D("10.4"), D("10"),
                     1_000_000)
    assert n is not None and n.startswith("OC-4")


@pytest.mark.rule("OC-1")
async def test_execution_refuses_insane_order(cfg: Config, risk: RiskManager) -> None:
    from trader.agents.a08_execution import ExecutionAgent
    from trader.brokers.sim import SimBroker

    s = make_setup("NVDA")
    d = risk.evaluate(s, make_ctx(), AGREE, OK, "p-sane")
    assert d.result == "APPROVE"
    broker = SimBroker(lambda: et(PLAIN_DAY, "09:45"))
    ex = ExecutionAgent(cfg, broker, None)
    code, ticket = await ex.execute(d, s, et(PLAIN_DAY, "09:45", 6), True, D("180.00"))
    assert code == Reason.EXEC_SANITY and ticket is None and "OC-1" in ex.last_sanity
