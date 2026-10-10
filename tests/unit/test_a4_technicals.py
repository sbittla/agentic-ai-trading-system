"""A4 technicals: levels (A4-1), indicators (A4-2), setups (A4-3), filters (A4-4).

Each playbook setup fires on a hand-built example and not on its counter-example
(M5 done criterion).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.conftest import PLAIN_DAY, bar, et
from trader.agents.a02_market_data import MarketDataAgent
from trader.agents.a04_technicals import TechnicalAgent, compute_levels
from trader.calendar import MacroCalendar, MarketCalendar
from trader.config import Config
from trader.models.messages import Bar, EventFlags, Levels, Quote, SetupTriggered


class Scenario:
    def __init__(self, cfg: Config, cal: MarketCalendar, macro: MacroCalendar,
                 d: date = PLAIN_DAY) -> None:
        self.cfg = cfg
        self.d = d
        syms = list(cfg.universe.tickers) + list(cfg.universe.context_symbols)
        self.md = MarketDataAgent(syms)
        self.ta = TechnicalAgent(cfg, self.md, cal, macro, require_quotes=False)
        for s, px in (("NVDA", 190.0), ("QQQ", 600.0), ("SMH", 310.0), ("AAPL", 250.0)):
            self.seed(s, px)
        self.md.start_day(d)
        self.ta.start_day(d)
        self.out: list[SetupTriggered] = []

    def seed(self, sym: str, px: float, half_range: float = 0.4, vol: int = 1000) -> None:
        days = []
        for k in range(20):
            d = self.d - timedelta(days=30 - k)
            t0 = et(d, "09:30")
            days.append([bar(sym, t0 + timedelta(minutes=m), px, px + half_range,
                             px - half_range, px, vol) for m in range(390)])
        self.md.seed_history(sym, days)
        self.md.state[sym].prior_close = px

    def levels(self, sym: str, **kw: float) -> None:
        base = dict(prior_high=192.8, prior_low=188.0, prior_close=189.0, ema20d=185.0,
                    ema50d=180.0, ema10d=187.0, atr14d=4.0, high20d=195.0,
                    close_high20d=194.0, avg_vol50d=200000.0, low5d=186.0)
        base.update(kw)
        self.ta.set_levels(Levels(day=self.d, ticker=sym, **base))   # type: ignore[arg-type]
        self.md.state[sym].prior_close = base["prior_close"]

    def feed(self, sym: str, start: str, closes: list[float], vols: list[int] | int = 3000,
             spread: float = 0.05, opens: list[float] | None = None) -> list[SetupTriggered]:
        t0 = et(self.d, start)
        prev = self.md.state[sym].last_price or closes[0]
        got: list[SetupTriggered] = []
        for i, c in enumerate(closes):
            o = opens[i] if opens else prev
            v = vols[i] if isinstance(vols, list) else vols
            b = bar(sym, t0 + timedelta(minutes=i), o, max(o, c) + spread,
                    min(o, c) - spread, c, v)
            got += self.step(b)
            prev = c
        return got

    def step(self, b: Bar) -> list[SetupTriggered]:
        got: list[SetupTriggered] = []
        for b5 in self.md.on_bar(b):
            if b5.symbol in self.cfg.universe.tickers:
                got += self.ta.on_5m_close(b5, b.end)
        if b.symbol in self.cfg.universe.tickers:
            got += self.ta.on_1m_close(b, b.end)
        self.out += got
        return got

    def market_up(self, start: str = "09:30", n: int = 30, qqq: float = 600.0) -> None:
        self.feed("QQQ", start, [qqq + 0.05 * i for i in range(n)], 3000)
        self.feed("SMH", start, [310 + 0.03 * i for i in range(n)], 3000)


@pytest.fixture
def sc(cfg, cal, macro) -> Scenario:  # type: ignore[no-untyped-def]
    return Scenario(cfg, cal, macro)


def orb_day(sc: Scenario, break_close: float = 190.62, break_vol: int = 4000) -> list:
    sc.levels("NVDA")
    sc.market_up(n=12)
    got = sc.feed("NVDA", "09:30", [190.0, 190.2, 190.3, 190.1, 190.2], 3000)
    got += sc.feed("NVDA", "09:35", [190.25, 190.3, 190.4, 190.5, break_close], break_vol)
    return got


@pytest.mark.rule("A4-3")
def test_orb_fires_on_closed_breakout(sc: Scenario) -> None:
    got = orb_day(sc)
    assert [s.setup for s in got] == ["ORB"]
    s = got[0]
    assert s.trigger == Decimal("190.35")    # OR5 high (190.30 + spread)
    assert s.ts == et(PLAIN_DAY, "09:40") and s.bar_closed
    assert s.stop < s.trigger < s.entry < s.t1 < s.t2
    assert s.entry - s.trigger <= Decimal(str(0.25 * s.atr5)) + Decimal("0.005")
    assert s.t2 - s.entry == (s.entry - s.stop) * 2 or abs(
        (s.t2 - s.entry) - 2 * (s.entry - s.stop)) <= Decimal("0.01")
    assert 0 <= s.score <= 100 and set(s.filters) == {"MF-1", "MF-2", "MF-3", "MF-4", "MF-5"}


@pytest.mark.rule("A4-3")
def test_orb_counter_examples(cfg, cal, macro) -> None:  # type: ignore[no-untyped-def]
    assert orb_day(Scenario(cfg, cal, macro), break_close=190.30) == []   # no close above
    assert orb_day(Scenario(cfg, cal, macro), break_vol=500) == []        # weak volume
    sc = Scenario(cfg, cal, macro)
    orb_day(sc)
    assert sc.feed("NVDA", "09:40", [190.4, 190.2, 190.3, 190.6, 190.9]) == []   # one-shot
    gap = Scenario(cfg, cal, macro)
    gap.levels("NVDA", prior_close=193.0)   # gap down > 1%
    gap.market_up(n=12)
    gap.feed("NVDA", "09:30", [190.0, 190.2, 190.3, 190.1, 190.2], 3000)
    assert gap.feed("NVDA", "09:35", [190.25, 190.3, 190.4, 190.5, 190.62], 4000) == []


@pytest.mark.rule("A4-3")
def test_gap_and_go(sc: Scenario) -> None:
    sc.levels("NVDA", prior_close=187.0)   # gap +1.6%
    sc.ta.news["NVDA"] = EventFlags(ticker="NVDA", updated_at=et(PLAIN_DAY, "07:15"),
                                    catalyst="supplier deal announced", tone="positive")
    sc.feed("NVDA", "08:00", [189.5, 190.0, 190.1], 500)   # pre-market high 190.15
    sc.market_up(n=12)
    sc.feed("NVDA", "09:30", [190.0, 190.2, 190.3, 190.1, 190.2], 3000)
    got = sc.feed("NVDA", "09:35", [190.25, 190.3, 190.4, 190.5, 190.62], 4000)
    names = {s.setup for s in got}
    assert "GAP_AND_GO" in names
    g = next(s for s in got if s.setup == "GAP_AND_GO")
    assert g.trigger == Decimal("190.35")   # max(pm high 190.15, OR5 high 190.35)


@pytest.mark.rule("A4-3")
def test_gap_and_go_needs_catalyst(sc: Scenario) -> None:
    sc.levels("NVDA", prior_close=187.0)
    sc.market_up(n=12)
    sc.feed("NVDA", "09:30", [190.0, 190.2, 190.3, 190.1, 190.2], 3000)
    got = sc.feed("NVDA", "09:35", [190.25, 190.3, 190.4, 190.5, 190.62], 4000)
    assert "GAP_AND_GO" not in {s.setup for s in got}


@pytest.mark.rule("A4-3")
def test_vwap_reclaim(sc: Scenario) -> None:
    sc.levels("NVDA")
    sc.market_up(n=40)
    sc.feed("NVDA", "09:30", [190.0, 190.3, 190.5, 190.4, 190.3], 3000)
    sc.feed("NVDA", "09:35", [190.0, 189.8, 189.6, 189.5, 189.6], 3000)   # closes below VWAP
    got = sc.feed("NVDA", "09:40", [189.9, 190.2, 190.4, 190.5, 190.6], 4000)  # reclaim
    assert "VWAP_RECLAIM" not in {s.setup for s in got}   # armed only: needs a later trade
    assert "VWAP_RECLAIM" in sc.ta.days["NVDA"].armed
    got = sc.feed("NVDA", "09:45", [190.8])
    assert [s.setup for s in got] == ["VWAP_RECLAIM"]
    assert got[0].stop < Decimal("189.6")


@pytest.mark.rule("A4-3")
def test_vwap_reclaim_needs_qqq_above_vwap(sc: Scenario) -> None:
    sc.levels("NVDA")
    sc.feed("QQQ", "09:30", [600 - 0.05 * i for i in range(20)], 3000)   # falling market
    sc.feed("NVDA", "09:30", [190.0, 190.3, 190.5, 190.4, 190.3], 3000)
    sc.feed("NVDA", "09:35", [190.0, 189.8, 189.6, 189.5, 189.6], 3000)
    sc.feed("NVDA", "09:40", [189.9, 190.2, 190.4, 190.5, 190.6], 4000)
    assert sc.feed("NVDA", "09:45", [190.8]) == []


@pytest.mark.rule("A4-3")
def test_first_pullback(sc: Scenario) -> None:
    sc.levels("NVDA", prior_high=199.0, high20d=200.0)
    sc.market_up(n=40)
    sc.feed("NVDA", "09:30", [190.0, 190.1, 190.2, 190.1, 190.2], 3000)
    sc.feed("NVDA", "09:35", [190.6, 191.0, 191.4, 191.8, 192.0], 9000)   # impulse, RVOL > 2
    got = sc.feed("NVDA", "09:40", [191.85, 191.8, 191.7, 191.75, 191.7], 2000,
                  opens=[191.9, 191.85, 191.8, 191.7, 191.75])   # pullback, lower high
    assert got == [] and "FIRST_PULLBACK" in sc.ta.days["NVDA"].armed
    got = sc.feed("NVDA", "09:45", [192.0])
    assert [s.setup for s in got] == ["FIRST_PULLBACK"]
    s = got[0]
    assert s.t2 >= Decimal(str(sc.md.state["NVDA"].hod or 0)) - Decimal("0.01")


@pytest.mark.rule("A4-3")
def test_power_hour_breakout(sc: Scenario) -> None:
    sc.levels("AAPL", prior_close=248.0, prior_high=249.0, high20d=260.0)
    sc.feed("QQQ", "09:30", [600 + 0.01 * i for i in range(330)], 3000)
    closes = [250 + 0.004 * i for i in range(330)]          # steady up-trend day
    sc.feed("AAPL", "09:30", closes, 3000)                  # until 15:00
    hod = sc.md.state["AAPL"].hod_before_1500
    assert hod is not None
    got = sc.feed("AAPL", "15:00", [closes[-1] + 0.06 * i for i in range(1, 6)], 6000)
    got = [s for s in got if s.setup == "POWER_HOUR_BREAKOUT"]
    assert len(got) == 1 and got[0].trigger == Decimal(str(round(hod, 2)))


@pytest.mark.rule("A4-3")
def test_power_hour_needs_trend_day(sc: Scenario) -> None:
    sc.levels("AAPL", prior_close=248.0)
    import math
    closes = [250 + 0.8 * math.sin(i / 7) for i in range(330)]   # chop around VWAP
    sc.feed("AAPL", "09:30", closes, 3000)
    got = sc.feed("AAPL", "15:00", [252 + 0.01 * i for i in range(5)], 6000)
    assert "POWER_HOUR_BREAKOUT" not in {s.setup for s in got}


@pytest.mark.rule("A4-3")
def test_vwap_trend(sc: Scenario) -> None:
    sc.levels("AAPL", prior_close=248.0, prior_high=249.0, high20d=270.0)
    closes = [250 + 0.01 * i for i in range(330)]
    sc.feed("AAPL", "09:30", closes, 3000)
    vw = sc.md.state["AAPL"].vwap_value or 0
    # pullback bar touching VWAP that closes above it (15:00-15:05)
    tb = [vw + 0.30, vw + 0.15, vw + 0.05, vw + 0.08, vw + 0.12]
    sc.feed("AAPL", "15:00", tb, 3000, opens=[closes[-1]] + tb[:-1])
    arm = sc.ta.days["AAPL"].armed["VWAP_TREND"]
    got = sc.feed("AAPL", "15:05", [arm.level + 0.1])
    assert [s.setup for s in got] == ["VWAP_TREND"]
    assert got[0].stop <= Decimal(str(round(vw, 2)))


@pytest.mark.rule("A4-3")
def test_swing_base_breakout(sc: Scenario) -> None:
    sc.levels("AAPL", prior_close=249.0, close_high20d=250.5, avg_vol50d=500000.0,
              ema20d=245.0, ema50d=240.0)
    closes = [249.5 + 0.0035 * i for i in range(375)]   # through 15:45
    got = sc.feed("AAPL", "09:30", closes, 1500)
    swings = [s for s in got if s.style == "swing"]
    assert "SWING_BASE_BREAKOUT" in {s.setup for s in swings}
    s = next(x for x in swings if x.setup == "SWING_BASE_BREAKOUT")
    assert s.ts == et(PLAIN_DAY, "15:45") and s.window == "afternoon"
    assert s.stop == Decimal("248.50")   # 250.5 - 0.5 x 4.0
    assert float(s.t1 - s.entry) == pytest.approx(float((s.entry - s.stop) * Decimal("1.5")),
                                                  abs=0.01)


@pytest.mark.rule("A4-3")
def test_swing_close_near_high(sc: Scenario) -> None:
    sc.levels("AAPL", prior_close=249.0, ema20d=248.5, ema50d=245.0, low5d=247.8,
              close_high20d=260.0)
    sc.ta.levels["AAPL"] = sc.ta.levels["AAPL"].model_copy(update={"pullback_to_ema": True})
    closes = [249.2 + 0.003 * i for i in range(375)]
    got = sc.feed("AAPL", "09:30", closes, 1500)
    assert "SWING_CLOSE_NEAR_HIGH" in {s.setup for s in got}


@pytest.mark.rule("A4-3")
def test_swing_excluded_before_earnings(sc: Scenario) -> None:
    sc.levels("AAPL", prior_close=249.0, close_high20d=250.5, avg_vol50d=500000.0)
    sc.ta.news["AAPL"] = EventFlags(ticker="AAPL", updated_at=et(PLAIN_DAY, "07:15"),
                                    flags=("EARNINGS_3D",))
    got = sc.feed("AAPL", "09:30", [249.5 + 0.0035 * i for i in range(375)], 1500)
    assert not [s for s in got if s.style == "swing"]


@pytest.mark.rule("A4-1")
def test_levels_from_daily_bars_no_lookahead() -> None:
    daily = [bar("AAPL", et(PLAIN_DAY - timedelta(days=60 - i), "09:30"), 100 + i, 101 + i,
                 99 + i, 100.5 + i, 1000, tf="1Day") for i in range(60)]
    daily.append(bar("AAPL", et(PLAIN_DAY, "09:30"), 500, 600, 400, 550, 1, tf="1Day"))
    lv = compute_levels("AAPL", PLAIN_DAY, daily)
    assert lv is not None and lv.prior_close == 159.5 and lv.prior_high == 160
    assert lv.history_days == 60 and lv.atr14d is not None and lv.ema50d is not None
    assert lv.high20d == 160 and lv.close_high20d == 159.5


@pytest.mark.rule("A4-2")
def test_intraday_indicators_on_closed_bars(sc: Scenario) -> None:
    sc.feed("AAPL", "09:30", [250 + 0.02 * i for i in range(30)])
    st = sc.md.state["AAPL"]
    assert st.vwap_value is not None and st.atr5.value is not None
    assert st.ema9_5m.value is not None and st.ema20_5m.value is not None
    assert st.rvol() == pytest.approx(3.0, rel=0.05)
    assert len(st.bars5) == 6


@pytest.mark.rule("A4-4")
@pytest.mark.rule("MF-1")
def test_mf1_market_filter(sc: Scenario) -> None:
    sc.levels("AAPL", prior_close=250.0)
    sc.feed("QQQ", "09:30", [600 - 0.1 * i for i in range(20)])   # below VWAP, falling
    sc.feed("AAPL", "09:30", [250 + 0.02 * i for i in range(20)])
    f = sc.ta.filters("AAPL", et(PLAIN_DAY, "09:50"), "ORB", 60)
    assert f["MF-1"] is False
    # A+ with relative strength may pass MF-1 (except VWAP_RECLAIM)
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:50"), "ORB", 85)["MF-1"] is True
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:50"), "VWAP_RECLAIM", 95)["MF-1"] is False


@pytest.mark.rule("MF-1")
def test_mf1_qqq_down_more_than_1pct(sc: Scenario) -> None:
    sc.md.state["QQQ"].prior_close = 610.0
    sc.feed("QQQ", "09:30", [600 + 0.05 * i for i in range(20)])   # above VWAP but -1.5%
    sc.feed("AAPL", "09:30", [250 + 0.02 * i for i in range(20)])
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:50"), "ORB", 95)["MF-1"] is False


@pytest.mark.rule("MF-2")
def test_mf2_semis_need_smh(sc: Scenario) -> None:
    sc.market_up(n=1)
    sc.feed("SMH", "09:31", [310 - 0.05 * i for i in range(20)])
    sc.feed("NVDA", "09:30", [190 + 0.02 * i for i in range(20)])
    sc.feed("AAPL", "09:30", [250 + 0.02 * i for i in range(20)])
    assert sc.ta.filters("NVDA", et(PLAIN_DAY, "09:50"), "ORB", 60)["MF-2"] is False
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:50"), "ORB", 60)["MF-2"] is True


@pytest.mark.rule("MF-3")
def test_mf3_rvol(sc: Scenario) -> None:
    sc.feed("AAPL", "09:30", [250 + 0.02 * i for i in range(20)], vols=1200)   # RVOL 1.2
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:50"), "ORB", 60)["MF-3"] is False
    sc2_vol = 2000
    sc.md.state["AAPL"].cum_vol = sc2_vol * 20
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:50"), "ORB", 60)["MF-3"] is True


@pytest.mark.rule("MF-4")
def test_mf4_spread(sc: Scenario) -> None:
    sc.ta.require_quotes = True
    sc.feed("AAPL", "09:30", [250 + 0.02 * i for i in range(10)])
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:40"), "ORB", 60)["MF-4"] is False   # no quote
    sc.md.on_quote(Quote(symbol="AAPL", ts=et(PLAIN_DAY, "09:40"), bid=250.0, ask=250.10))
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:40"), "ORB", 60)["MF-4"] is True   # 0.04%
    sc.md.on_quote(Quote(symbol="AAPL", ts=et(PLAIN_DAY, "09:40"), bid=250.0, ask=250.20))
    assert sc.ta.filters("AAPL", et(PLAIN_DAY, "09:40"), "ORB", 60)["MF-4"] is False   # 0.08%


@pytest.mark.rule("MF-5")
def test_mf5_halt_stale(sc: Scenario) -> None:
    from trader.models.messages import TradingStatus

    sc.feed("AAPL", "09:30", [250 + 0.02 * i for i in range(10)])
    now = et(PLAIN_DAY, "09:40")
    assert sc.ta.filters("AAPL", now, "ORB", 60)["MF-5"] is True
    sc.md.on_status(TradingStatus(symbol="AAPL", ts=now, kind="luld"))
    assert sc.ta.filters("AAPL", now + timedelta(minutes=5), "ORB", 60)["MF-5"] is False
    assert sc.ta.filters("AAPL", now + timedelta(minutes=11), "ORB", 60)["MF-5"] is False  # stale


@pytest.mark.rule("A5-1")
def test_session_screen_candidates_from_latest_session() -> None:
    """Preview plan screen: uptrend closing at its high near the 20-day closing high gives
    candidates whose numbers are all in the output (PR-4); a downtrend gives none."""
    from trader.agents.a04_technicals import session_screen

    up = [bar("AAPL", et(PLAIN_DAY - timedelta(days=60 - i), "09:30"), 100 + i, 101.5 + i,
              99 + i, 101 + i, 1000, tf="1Day") for i in range(60)]
    lv = compute_levels("AAPL", PLAIN_DAY, up)
    assert lv is not None
    scr = session_screen(up, lv)
    assert scr["trend_up"] and scr["close"] == 160 and scr["range_pos"] == 0.8
    assert scr["change_pct"] == pytest.approx(0.63, abs=0.01)
    setups = {c["setup"]: c for c in scr["candidates"]}
    assert {"ORB", "SWING_BASE_BREAKOUT"} <= set(setups)
    orb = setups["ORB"]
    assert orb["trigger"] == 160.5 and orb["stop"] < orb["trigger"] < orb["target_1"]
    assert all(isinstance(orb[k], float) for k in ("trigger", "stop", "target_1"))
    assert scr["readiness_score"] >= 50

    down = [bar("AAPL", et(PLAIN_DAY - timedelta(days=60 - i), "09:30"), 200 - i, 201 - i,
                198.5 - i, 199 - i, 1000, tf="1Day") for i in range(60)]
    lv = compute_levels("AAPL", PLAIN_DAY, down)
    assert lv is not None
    scr = session_screen(down, lv)
    assert not scr["trend_up"] and scr["candidates"] == []
