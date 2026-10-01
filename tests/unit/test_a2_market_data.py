"""A2 market data (A2-1..A2-5, DQ-3, DQ-5) and indicator reference values."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import PLAIN_DAY, bar, et, minute_series
from trader.agents.a02_market_data import MarketDataAgent
from trader.indicators import (
    Ema,
    RvolProfile,
    WilderAtr,
    atr_series,
    bollinger,
    cmf,
    ema_series,
    macd,
    obv,
    rsi,
    vwap,
)
from trader.models.messages import Quote, TradingStatus


@pytest.mark.rule("A2-1")
def test_streams_ten_tickers_plus_context(cfg) -> None:  # type: ignore[no-untyped-def]
    symbols = list(cfg.universe.tickers) + list(cfg.universe.context_symbols)
    md = MarketDataAgent(symbols)
    assert len(md.symbols) == 13 <= 30
    md.on_quote(Quote(symbol="QQQ", ts=et(PLAIN_DAY, "09:31"), bid=600, ask=600.02))
    assert md.state["QQQ"].quote is not None


@pytest.mark.rule("A2-2")
def test_opening_ranges_and_vwap() -> None:
    md = MarketDataAgent(["AAPL"])
    md.start_day(PLAIN_DAY)
    closes = [100 + 0.1 * i for i in range(16)]
    closed5 = []
    for b in minute_series("AAPL", PLAIN_DAY, "09:30", closes):
        closed5 += md.on_bar(b)
    st = md.state["AAPL"]
    assert st.or5 == (pytest.approx(100.45), pytest.approx(99.95))
    assert st.or15 is not None and st.or15[0] == pytest.approx(101.45)
    assert len(closed5) == 3 and closed5[0].timeframe == "5Min"
    assert closed5[0].ts == et(PLAIN_DAY, "09:30")
    b1 = st.bars1
    assert st.vwap_value == pytest.approx(vwap([b.h for b in b1], [b.l for b in b1],
                                               [b.c for b in b1], [b.v for b in b1]))


@pytest.mark.rule("A2-2")
def test_premarket_tracked_separately() -> None:
    md = MarketDataAgent(["AAPL"])
    md.start_day(PLAIN_DAY)
    md.on_bar(bar("AAPL", et(PLAIN_DAY, "08:00"), 101, 102, 100.5, 101.5))
    md.on_bar(bar("AAPL", et(PLAIN_DAY, "09:00"), 101.5, 102.5, 101, 102))
    st = md.state["AAPL"]
    assert (st.pm_high, st.pm_low) == (102.5, 100.5) and st.bars1 == []


@pytest.mark.rule("A2-3")
def test_bar_store_keeps_history(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from trader.data.store import BarStore

    store = BarStore(tmp_path)
    bars = minute_series("AAPL", PLAIN_DAY, "09:30", [100, 101, 102])
    store.write("minute", "AAPL", bars)
    store.write("minute", "AAPL", bars)   # idempotent
    assert [b.c for b in store.read("minute", "AAPL")] == [100, 101, 102]


@pytest.mark.rule("A2-4")
def test_stale_after_60_seconds() -> None:
    md = MarketDataAgent(["AAPL"], stale_after_sec=60)
    t = et(PLAIN_DAY, "09:40")
    md.on_bar(bar("AAPL", t, 100, 100.1, 99.9, 100))
    assert not md.is_stale(t + timedelta(minutes=1, seconds=59))
    assert md.is_stale(t + timedelta(minutes=2, seconds=1))


@pytest.mark.rule("A2-5")
def test_rvol_against_same_feed_history() -> None:
    prof = RvolProfile()
    for _ in range(20):
        prof.add_session([(m, 1000.0) for m in range(390)])
    assert prof.rvol(9, 15000) == pytest.approx(1.5)   # 10 minutes x 1000 avg
    assert prof.fraction_done(194) == pytest.approx(0.5)


@pytest.mark.rule("A2-4")
def test_halts_and_luld_tracked() -> None:
    md = MarketDataAgent(["AAPL"])
    t = et(PLAIN_DAY, "09:50")
    md.on_status(TradingStatus(symbol="AAPL", ts=t, kind="halt"))
    assert md.recent_halt("AAPL", t, 10)
    md.on_status(TradingStatus(symbol="AAPL", ts=t + timedelta(minutes=5), kind="resume"))
    assert md.recent_halt("AAPL", t + timedelta(minutes=14), 10)
    assert not md.recent_halt("AAPL", t + timedelta(minutes=16), 10)


def test_dq3_bad_print_held_then_dropped() -> None:
    md = MarketDataAgent(["AAPL"])
    md.start_day(PLAIN_DAY)
    for b in minute_series("AAPL", PLAIN_DAY, "09:30", [100, 100.1, 100.2]):
        md.on_bar(b)
    md.on_bar(bar("AAPL", et(PLAIN_DAY, "09:33"), 100.2, 120, 100.2, 119))   # bad print
    assert md.state["AAPL"].last_price == 100.2
    md.on_bar(bar("AAPL", et(PLAIN_DAY, "09:34"), 100.2, 100.4, 100.1, 100.3))
    st = md.state["AAPL"]
    assert st.last_price == 100.3 and max(b.h for b in st.bars1) < 101


@pytest.mark.rule("A2-1")
def test_gap_marks_incomplete_and_backfill_recovers() -> None:
    """A2 done-when: a missed message is detected and back-filled."""
    md = MarketDataAgent(["AAPL"])
    md.start_day(PLAIN_DAY)
    series = minute_series("AAPL", PLAIN_DAY, "09:30", [100 + 0.05 * i for i in range(12)])
    for b in series[:3]:
        md.on_bar(b)
    md.on_bar(series[8])   # 5 minutes missed
    assert md.state["AAPL"].incomplete
    md.backfill(series[3:8])
    assert not md.state["AAPL"].incomplete


def test_indicator_reference_values() -> None:
    xs = [float(x) for x in range(1, 31)]
    e = ema_series(xs, 10)
    assert e[8] is None and e[9] == pytest.approx(5.5)
    assert e[10] == pytest.approx(5.5 * (1 - 2 / 11) + 11 * 2 / 11)
    a = WilderAtr(3)
    assert a.update(10, 8, 9) is None
    a.update(11, 9, 10)
    assert a.update(12, 10, 11) == pytest.approx(2.0)
    assert a.update(14, 11, 13) == pytest.approx((2 * 2 + 3) / 3)
    assert atr_series([2, 3, 4], [1, 1, 1], [1.5, 2, 3], 2)[1] == pytest.approx(1.5)
    assert rsi(xs) == 100.0
    assert macd(xs) is None and macd(xs + [31.0 + i for i in range(10)]) is not None
    assert bollinger(xs) is not None
    assert obv([1, 2, 1, 3], [10, 20, 30, 40]) == 30
    assert cmf([2] * 20, [1] * 20, [2] * 20, [100] * 20) == pytest.approx(1.0)
    ema = Ema(2)
    ema.update(1)
    assert ema.update(3) == pytest.approx(2.0)
