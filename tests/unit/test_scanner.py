"""Technical scanner (docs/SCANNER_REQUIREMENTS.md TS-1..TS-16)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trader import config as config_mod
from trader.scanner import patterns as pat
from trader.scanner import ta
from trader.scanner import techniques as T
from trader.scanner.evaluate import (
    Graded,
    Outcome,
    edges_from,
    grade,
    load_edges,
    report,
    walk_forward,
)
from trader.scanner.explain import explain, numbers_ok
from trader.scanner.inplay import evaluate_in_play, expected_range, in_play
from trader.scanner.report import render
from trader.scanner.techniques import Frame, Idea, Series, ideas_at, scan

# --- builders ------------------------------------------------------------------------------


def _days(n: int, start: date = date(2024, 1, 2)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def mk(closes: list[float], *, highs: list[float] | None = None,
       lows: list[float] | None = None, opens: list[float] | None = None,
       vols: list[float] | None = None, sym: str = "TST") -> Series:
    """Daily series: open = prior close, high/low 0.5 beyond the body unless given."""
    n = len(closes)
    o = opens or [closes[0]] + closes[:-1]
    h = highs or [max(a, b) + 0.5 for a, b in zip(o, closes, strict=True)]
    lo = lows or [min(a, b) - 0.5 for a, b in zip(o, closes, strict=True)]
    v = vols or [1e6] * n
    rows = list(zip(_days(n), o, h, lo, closes, v, strict=True))
    return Series.from_rows(sym, rows)


def reflect(s: Series, p0: float = 500.0) -> Series:
    """Price mirror around p0: an uptrend becomes the matching downtrend."""
    return Series(s.symbol, s.dates, 2 * p0 - s.o, 2 * p0 - s.l, 2 * p0 - s.h, 2 * p0 - s.c,
                  s.v)


def random_walk(n: int = 400, seed: int = 7, sym: str = "RW", drift: float = 0.0004
                ) -> Series:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.018, n)))
    o = np.concatenate([[c[0]], c[:-1]]) * (1 + rng.normal(0, 0.004, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.008, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.008, n)))
    v = rng.integers(500_000, 3_000_000, n).astype(float)
    return Series.from_rows(sym, list(zip(_days(n), o, h, lo, c, v, strict=True)))


def uptrend(n: int = 260, step: float = 0.5, base: float = 100.0) -> list[float]:
    return [base + step * i for i in range(n)]


def pullback_series() -> Series:
    c = uptrend()
    c += [c[-1] - 1.5, c[-1] - 3.0, c[-1] - 4.5, c[-1] - 3.0]
    return mk(c)


def mk_idea(side: str = "long", entry: str = "stop", trigger: float = 100.0,
            stop: float = 98.0, m1: float = 1.5, m2: float = 3.0) -> Idea:
    r = trigger - stop
    sgn = 1 if side == "long" else -1
    if side == "short":
        trigger, stop = 2 * 100 - trigger, 2 * 100 - stop
    return Idea(symbol="TST", asof=date(2026, 1, 5), technique="X", style="swing",
                side=side, entry=entry, trigger=trigger, stop=stop,  # type: ignore[arg-type]
                t1=trigger + sgn * m1 * r, t2=trigger + sgn * m2 * r, risk=r, room_r=None,
                score=50, why="")


def bars(*rows: tuple[float, float, float, float]) -> list[list[tuple[float, float, float,
                                                                         float]]]:
    """One daily bar per session."""
    return [[r] for r in rows]


def mirror(*rows: tuple[float, float, float, float]) -> list[list[tuple[float, float, float,
                                                                           float]]]:
    """The same sessions reflected around 100 (for short-side grading)."""
    return [[(200 - o, 200 - lo, 200 - h, 200 - c)] for o, h, lo, c in rows]


# --- TS-1, TS-2: indicators ----------------------------------------------------------------


@pytest.mark.rule("TS-2")
def test_indicator_known_values() -> None:
    x = np.arange(1.0, 31.0)
    assert ta.sma(x, 5)[4] == 3.0 and np.isnan(ta.sma(x, 5)[3])
    e = ta.ema(x, 3)
    assert e[2] == 2.0 and e[3] == pytest.approx(3.0)      # SMA seed, then alpha 0.5
    assert ta.rsi(x, 14)[-1] == 100.0                        # only gains
    assert ta.rsi(x[::-1].copy(), 14)[-1] == 0.0             # only losses
    h, lo, c = x + 1, x - 1, x
    assert ta.atr(h, lo, c, 14)[14] == pytest.approx(2.0)    # TR = 2 every bar
    up, mid, dn = ta.bbands(np.full(30, 5.0))
    assert up[-1] == mid[-1] == dn[-1] == 5.0
    assert ta.obv(np.array([1.0, 2, 1, 1]), np.array([10.0, 5, 3, 7]))[-1] == 12.0


@pytest.mark.rule("TS-2")
def test_indicators_match_talib_exactly() -> None:
    talib = pytest.importorskip("talib")
    s = random_walk(500, seed=11)
    o, h, lo, c, v = s.o, s.h, s.l, s.c, s.v

    def same(a: Any, b: Any) -> None:
        a, b = np.asarray(a, float), np.asarray(b, float)
        assert np.array_equal(np.isnan(a), np.isnan(b))
        m = ~np.isnan(a)
        assert np.allclose(a[m], b[m], rtol=0, atol=1e-8)

    same(ta.sma(c, 20), talib.SMA(c, 20))
    same(ta.ema(c, 20), talib.EMA(c, 20))
    same(ta.rsi(c, 14), talib.RSI(c, 14))
    same(ta.rsi(c, 2), talib.RSI(c, 2))
    same(ta.atr(h, lo, c, 14), talib.ATR(h, lo, c, 14))
    for mine, ref in zip(ta.macd(c), talib.MACD(c, 12, 26, 9), strict=True):
        same(mine, ref)
    for mine, ref in zip(ta.bbands(c, 20, 2.0), talib.BBANDS(c, 20, 2, 2), strict=True):
        same(mine, ref)
    a, p, m = ta.adx(h, lo, c, 14)
    same(a, talib.ADX(h, lo, c, 14))
    same(p, talib.PLUS_DI(h, lo, c, 14))
    same(m, talib.MINUS_DI(h, lo, c, 14))
    k, d = ta.stoch(h, lo, c, 14, 3, 3)
    rk, rd = talib.STOCH(h, lo, c, fastk_period=14, slowk_period=3, slowk_matype=0,
                         slowd_period=3, slowd_matype=0)
    same(k, rk)
    same(d, rd)
    same(ta.obv(c, v), talib.OBV(c, v))
    same(ta.roc(c, 10), talib.ROC(c, 10))
    assert len(o) == 500


# --- TS-3: patterns -------------------------------------------------------------------------


@pytest.mark.rule("TS-3")
def test_candlestick_patterns_match_talib_exactly() -> None:
    talib = pytest.importorskip("talib")
    for seed in (1, 2, 3):
        s = random_walk(600, seed=seed)
        o, h, lo, c = s.o, s.h, s.l, s.c
        assert np.array_equal(pat.engulfing(o, h, lo, c), talib.CDLENGULFING(o, h, lo, c))
        assert np.array_equal(pat.hammer(o, h, lo, c), talib.CDLHAMMER(o, h, lo, c))
        assert np.array_equal(pat.shooting_star(o, h, lo, c),
                              talib.CDLSHOOTINGSTAR(o, h, lo, c))
        assert np.array_equal(pat.doji(o, h, lo, c), talib.CDLDOJI(o, h, lo, c))
        star = pat.morning_evening_star(o, h, lo, c)
        assert np.array_equal(np.where(star > 0, 100, 0), talib.CDLMORNINGSTAR(o, h, lo, c))
        assert np.array_equal(np.where(star < 0, -100, 0), talib.CDLEVENINGSTAR(o, h, lo, c))
        pd_ = pat.piercing_darkcloud(o, h, lo, c)
        assert np.array_equal(np.where(pd_ > 0, 100, 0), talib.CDLPIERCING(o, h, lo, c))
        assert np.array_equal(np.where(pd_ < 0, -100, 0), talib.CDLDARKCLOUDCOVER(o, h, lo, c))


@pytest.mark.rule("TS-3")
def test_pattern_fixtures() -> None:
    # bullish engulfing: black bar, then a white body covering it
    o, c = np.array([10.0, 9.0]), np.array([9.5, 10.5])
    h, lo = np.array([10.1, 10.6]), np.array([9.4, 8.9])
    assert list(pat.engulfing(o, h, lo, c)) == [0, 100]
    assert list(pat.engulfing(-o, -lo, -h, -c)) == [0, -100]          # mirrored: bearish
    # inside bar and NR7
    h = np.array([10, 11, 12, 13, 14, 15, 15.0])
    lo = np.array([8, 9, 10, 11, 12, 13, 14.5])
    assert pat.inside_bar(h, lo)[-1]
    assert pat.nr(h, lo, 7)[-1] and not pat.nr(h, lo, 7)[-2]
    assert not pat.inside_bar(np.array([10.0, 10.5]), np.array([9.0, 9.5]))[-1]


# --- TS-4: techniques ---------------------------------------------------------------------


@pytest.mark.rule("TS-4")
def test_trend_pullback_fires_and_its_mirror_is_the_short() -> None:
    s = pullback_series()
    i = len(s.c) - 1
    cand = T.trend_pullback(Frame(s, None, +1), i)
    assert cand is not None and "EMA20" in cand.why
    longs = [d for d in ideas_at(T.frames(s, None), i, dedup=False)
             if d.technique == "TREND_PULLBACK"]
    shorts = [d for d in ideas_at(T.frames(reflect(s), None), i, dedup=False)
              if d.technique == "TREND_PULLBACK"]
    assert len(longs) == 1 and len(shorts) == 1
    lg, sh = longs[0], shorts[0]
    assert lg.side == "long" and sh.side == "short"
    for a, b in ((lg.trigger, sh.trigger), (lg.stop, sh.stop), (lg.t1, sh.t1), (lg.t2, sh.t2)):
        assert b == pytest.approx(1000 - a, abs=0.011)
    assert sh.score == lg.score


@pytest.mark.rule("TS-4")
def test_trend_pullback_needs_a_pullback() -> None:
    s = mk(uptrend(265))                       # still making highs: no pullback
    assert T.trend_pullback(Frame(s, None, +1), len(s.c) - 1) is None


@pytest.mark.rule("TS-4")
def test_breakout_needs_volume() -> None:
    base = uptrend(260)
    c = base + [base[-1]] * 25 + [base[-1] + 2.0]
    vols = [1e6] * (len(c) - 1) + [3e6]
    i = len(c) - 1
    s = mk(c, vols=vols)
    cand = T.breakout_20d(Frame(s, None, +1), i)
    assert cand is not None and "3.0x volume" in cand.why
    assert T.breakout_20d(Frame(mk(c), None, +1), i) is None          # 1x volume: no
    short = T.breakout_20d(Frame(reflect(s), None, -1), i)
    assert short is not None and short.trigger == pytest.approx(cand.trigger - 1000)


@pytest.mark.rule("TS-4")
def test_rsi2_reversion_buys_the_dip_in_an_uptrend() -> None:
    c = uptrend(260)
    c += [c[-1] - 2, c[-1] - 4, c[-1] - 6]
    s = mk(c)
    cand = T.rsi2_reversion(Frame(s, None, +1), len(c) - 1)
    assert cand is not None and cand.entry == "open" and "Connors" in cand.why
    # the same dip below the 200-day average is not a long
    d = [300 - 0.5 * i for i in range(260)]
    d += [d[-1] - 2, d[-1] - 4, d[-1] - 6]
    assert T.rsi2_reversion(Frame(mk(d), None, +1), len(d) - 1) is None


@pytest.mark.rule("TS-4")
def test_inside_nr7_and_bull_flag() -> None:
    c = uptrend(262)
    o = [c[0]] + c[:-1]
    h = [max(a, b) + 0.5 for a, b in zip(o, c, strict=True)]
    lo = [min(a, b) - 0.5 for a, b in zip(o, c, strict=True)]
    o[-1], c[-1], h[-1], lo[-1] = c[-2] - 0.1, c[-2], c[-2] + 0.1, c[-2] - 0.2   # inside, narrow
    s = mk(c, highs=h, lows=lo, opens=o)
    assert T.inside_nr7(Frame(s, None, +1), len(c) - 1) is not None

    c = [100 + 0.1 * i for i in range(260)]
    vols = [1e6] * 260
    for _ in range(6):                     # pole: +3 a day on heavy volume
        c.append(c[-1] + 3)
        vols.append(2e6)
    for _ in range(5):                     # flag: drifting lower on light volume
        c.append(c[-1] - 0.5)
        vols.append(5e5)
    s = mk(c, vols=vols)
    cand = T.bull_flag(Frame(s, None, +1), len(c) - 1)
    assert cand is not None and "flag" in cand.why
    heavy = mk(c, vols=vols[:-5] + [3e6] * 5)                   # flag volume too heavy
    assert T.bull_flag(Frame(heavy, None, +1), len(c) - 1) is None


@pytest.mark.rule("TS-4")
def test_reversal_candle_needs_support() -> None:
    c = uptrend(260)
    last = c[-1]
    # day -2: black bar down into EMA20; day -1: bullish engulfing at the EMA20
    c += [last - 4.0, last + 0.2]
    o = [c[0]] + c[:-1]
    o[-2], o[-1] = last, last - 4.6
    h = [max(a, b) + 0.5 for a, b in zip(o, c, strict=True)]
    lo = [min(a, b) - 0.5 for a, b in zip(o, c, strict=True)]
    s = mk(c, highs=h, lows=lo, opens=o)
    fr = Frame(s, None, +1)
    i = len(c) - 1
    assert fr.reversal["bullish engulfing"][i]
    cand = T.reversal_candle(fr, i)
    assert cand is not None and "bullish engulfing" in cand.why


@pytest.mark.rule("TS-4")
def test_every_technique_has_a_registry_entry_and_short_name() -> None:
    names = T.technique_names()
    assert len(T.TECHNIQUES) == 9 and len(set(names)) == len(names)
    assert {"BREAKDOWN_20D", "LOW_52W", "BEAR_FLAG"} <= set(names)


# --- TS-5: day watchlist --------------------------------------------------------------------


@pytest.mark.rule("TS-5")
def test_in_play_ranks_by_expected_range_with_levels() -> None:
    calm = random_walk(300, seed=3, sym="CALM")
    wild = Series("WILD", calm.dates, calm.o, calm.c + (calm.h - calm.c) * 3,
                  calm.c - (calm.c - calm.l) * 3, calm.c, calm.v)
    plays = in_play({"CALM": calm, "WILD": wild})
    assert [p.symbol for p in plays] == ["WILD", "CALM"] and plays[0].rank == 1
    p = plays[1]
    assert p.levels["prior_high"] == round(float(calm.h[-1]), 2)
    assert p.levels["prior_low"] == round(float(calm.l[-1]), 2)
    assert p.bias in ("long", "short", "neutral") and p.plan
    assert expected_range(2.0, 1.0, 1.0) == 2.0
    assert expected_range(2.0, 4.0, 3.0) == pytest.approx(2.0 * 1.6)        # clamped


@pytest.mark.rule("TS-5")
def test_in_play_evaluation_measures_lift() -> None:
    s = {f"S{k}": random_walk(320, seed=k, sym=f"S{k}") for k in range(6)}
    # make one symbol twice as volatile: it should carry the lift
    v = s["S0"]
    s["S0"] = Series("S0", v.dates, v.o, v.c + (v.h - v.c) * 4, v.c - (v.c - v.l) * 4, v.c,
                     v.v)
    res = evaluate_in_play(s, top_n=2)
    assert res["days"] > 50 and res["lift"] > 1.0 and 0 < res["calibration"] < 3


# --- TS-6..TS-9: context, idea fields, score, dedup ------------------------------------------


@pytest.mark.rule("TS-6")
def test_market_regime_and_relative_strength() -> None:
    s = pullback_series()
    spy_up = mk(uptrend(len(s.c), step=0.1), sym="SPY")
    spy_down = mk([300 - 0.1 * i for i in range(len(s.c))], sym="SPY")
    i = len(s.c) - 1
    up = Frame(s, spy_up, +1)
    down = Frame(s, spy_down, +1)
    assert up.spy_up[i] == 1.0 and down.spy_up[i] == -1.0
    assert up.rs63[i] > 0 and Frame(s, spy_up, -1).rs63[i] < 0
    a = [d for d in ideas_at(T.frames(s, spy_up), i, dedup=False) if d.side == "long"][0]
    b = [d for d in ideas_at(T.frames(s, spy_down), i, dedup=False) if d.side == "long"][0]
    assert a.factors["market"] > 0 > b.factors["market"] and a.score > b.score


@pytest.mark.rule("TS-7")
def test_idea_fields_and_targets() -> None:
    s = pullback_series()
    d = [x for x in ideas_at(T.frames(s, None), len(s.c) - 1, dedup=False)
         if x.technique == "TREND_PULLBACK"][0]
    r = d.trigger - d.stop                      # the printed plan adds up to the cent
    assert d.risk == pytest.approx(r, abs=1e-9)
    assert d.t1 == pytest.approx(d.trigger + 1.5 * r, abs=0.006)
    assert d.t2 == pytest.approx(d.trigger + 3.0 * r, abs=0.006)
    assert d.entry == "stop" and d.style == "swing" and d.asof == s.dates[-1]
    js = d.as_dict()
    assert js["asof"] == s.dates[-1].isoformat() and "index" not in js


@pytest.mark.rule("TS-7")
def test_stop_too_wide_or_too_tight_is_no_idea() -> None:
    s = pullback_series()
    i = len(s.c) - 1
    wide = T.Technique("WIDE", "swing", "trend",
                       lambda x, i: T.Cand(float(x.h[i]), float(x.h[i]) - 3 * float(x.atr[i]),
                                           "w"), "")
    tight = T.Technique("TIGHT", "swing", "trend",
                        lambda x, i: T.Cand(float(x.h[i]), float(x.h[i]) - 0.1 * float(x.atr[i]),
                                            "t"), "")
    old = T.TECHNIQUES
    try:
        T.TECHNIQUES = (wide, tight)
        assert ideas_at(T.frames(s, None), i, dedup=False) == []
    finally:
        T.TECHNIQUES = old


@pytest.mark.rule("TS-8")
def test_score_uses_measured_edge_and_penalises_overheated_trends() -> None:
    s = pullback_series()
    i = len(s.c) - 1
    base = [d for d in ideas_at(T.frames(s, None), i, dedup=False)
            if d.technique == "TREND_PULLBACK"][0]
    good = [d for d in ideas_at(T.frames(s, None), i, dedup=False,
                                edges={"TREND_PULLBACK:long": 0.15})
            if d.technique == "TREND_PULLBACK"][0]
    bad = [d for d in ideas_at(T.frames(s, None), i, dedup=False,
                               edges={"TREND_PULLBACK:long": -0.5})
           if d.technique == "TREND_PULLBACK"][0]
    assert good.factors["edge"] == 15 and bad.factors["edge"] == -20
    assert bad.score < base.score < good.score
    # a straight-line trend has ADX near 100: overheated
    assert base.factors.get("overheated") == -10
    assert 0 <= bad.score <= 100


@pytest.mark.rule("TS-8")
def test_render_applies_min_score_and_top_n() -> None:
    s = pullback_series()
    ideas, _ = scan({"TST": s}, None)
    low = [replace(d, score=10) for d in ideas]
    text = render(s.dates[-1], low, [], {}, top_n=5, in_play_n=5, min_score=50)
    assert "none qualify today" in text and "lower-scoring" in text
    text = render(s.dates[-1], ideas, [], {}, top_n=5, in_play_n=5, min_score=0)
    assert ideas[0].symbol in text and "buy-stop" in text


@pytest.mark.rule("TS-9")
def test_one_idea_per_symbol_best_score_first() -> None:
    s = pullback_series()
    i = len(s.c) - 1
    every = ideas_at(T.frames(s, None), i, dedup=False)
    best = ideas_at(T.frames(s, None), i)
    assert len(best) == 1 and best[0].score == max(d.score for d in every)
    a, b = random_walk(400, seed=5, sym="AAA"), random_walk(400, seed=6, sym="BBB")
    ranked, _ = scan({"AAA": a, "BBB": b}, None, disabled=frozenset())
    assert len({d.symbol for d in ranked}) == len(ranked)
    assert [d.score for d in ranked] == sorted((d.score for d in ranked), reverse=True)


# --- TS-1, TS-10: determinism and no look-ahead ---------------------------------------------


@pytest.mark.rule("TS-1")
def test_same_bars_same_ideas() -> None:
    a = {f"S{k}": random_walk(420, seed=k, sym=f"S{k}") for k in range(4)}
    spy = random_walk(420, seed=99, sym="SPY")
    one, _ = scan(a, spy)
    two, _ = scan(a, spy)
    assert [x.as_dict() for x in one] == [x.as_dict() for x in two]


@pytest.mark.rule("TS-10")
def test_no_look_ahead() -> None:
    s = random_walk(420, seed=21)
    spy = random_walk(420, seed=22, sym="SPY")
    full = T.frames(s, spy)
    fired = 0
    for i in range(T.MIN_HISTORY, 419, 7):
        cut = s.upto(i + 1)
        part = T.frames(cut, spy)
        a = [x.as_dict() for x in ideas_at(full, i, dedup=False)]
        b = [x.as_dict() for x in ideas_at(part, i, dedup=False)]
        assert a == b, s.dates[i]
        fired += len(a)
    assert fired > 0


# --- TS-11: grading -------------------------------------------------------------------------


@pytest.mark.rule("TS-11")
def test_grading_paths() -> None:
    long = mk_idea()                                     # trigger 100, stop 98, T1 103, T2 106
    assert not grade(long, bars((99, 99.5, 98.5, 99))).filled
    win = grade(long, bars((99.5, 101, 99.2, 100.5), (101, 103.5, 100.5, 103),
                           (103, 106.5, 102.5, 106)))
    assert win.filled and win.exit == "t2" and win.r == pytest.approx(0.75 + 1.5)
    assert win.plus1r_first and win.t1_hit
    be = grade(long, bars((99.5, 101, 99.2, 100.5), (101, 103.5, 100.5, 103),
                          (103, 103.2, 99.5, 100)))
    assert be.exit == "breakeven" and be.r == pytest.approx(0.75)
    ambiguous = grade(long, bars((99.5, 104, 97.5, 103)))           # stop and T1 same bar
    assert ambiguous.exit == "stop" and ambiguous.r == pytest.approx(-1.0)
    timed = grade(long, bars((99.5, 100.5, 99.5, 100.2), (100.2, 101, 99.8, 101)))
    assert timed.exit == "time" and timed.r == pytest.approx(0.5)


@pytest.mark.rule("TS-11")
def test_gaps_no_chase_and_gap_through_stop() -> None:
    long = mk_idea()
    chase = grade(long, bars((101.5, 102, 101, 101.8)))           # open > trigger + 0.5R
    assert not chase.filled and chase.exit == "gap_chase"
    gap_fill = grade(long, bars((100.6, 101, 100.4, 100.8)))       # within 0.5R: fill at open
    assert gap_fill.filled and gap_fill.r == pytest.approx((100.8 - 100.6) / (100.6 - 98), abs=1e-3)
    gap_stop = grade(long, bars((99.5, 100.5, 99.5, 100), (97, 97.5, 96, 96.5)))
    assert gap_stop.exit == "gap_stop" and gap_stop.r == pytest.approx(-1.5)


@pytest.mark.rule("TS-11")
def test_open_entry_and_short_side_mirror() -> None:
    op = mk_idea(entry="open")
    res = grade(op, bars((99, 103.2, 98.5, 103)))
    assert res.filled and res.exit == "time" and res.t1_hit      # filled at 99, R = 1
    short = mk_idea(side="short")
    sessions = mirror((99.5, 101, 99.2, 100.5), (101, 103.5, 100.5, 103),
                      (103, 106.5, 102.5, 106))
    res = grade(short, sessions)
    assert res.exit == "t2" and res.r == pytest.approx(2.25)


@pytest.mark.rule("TS-11")
def test_minute_bars_resolve_the_ambiguous_day() -> None:
    long = mk_idea()
    daily = grade(long, bars((99.5, 106.5, 97.5, 105)))
    assert daily.exit == "stop"
    minutes = [[(99.5, 100.2, 99.4, 100.1), (100.1, 103.2, 100.0, 103.1),
                (103.1, 106.4, 103.0, 106.2), (106.2, 106.5, 97.5, 105)]]
    res = grade(long, minutes, [True])
    assert res.exit == "t2" and res.minute_resolved and res.r == pytest.approx(2.25)


# --- TS-12: report and edges ----------------------------------------------------------------


@pytest.mark.rule("TS-12")
def test_report_flags_weak_techniques_and_buckets() -> None:
    def g(tech: str, side: str, r: float, score: int = 50) -> Graded:
        idea = replace(mk_idea(side=side), technique=tech, score=score)
        return Graded(idea, Outcome(True, r, r > 0, r > 0, "time", 3))

    rows = [g("BAD", "long", -0.5) for _ in range(30)] + [g("GOOD", "long", 0.4, 72)
                                                          for _ in range(30)]
    rows += [Graded(mk_idea(), Outcome(False))]
    rep = report(rows, min_samples=30)
    assert rep["weak"] == ["BAD:long"]
    good = next(t for t in rep["techniques"] if t["technique"] == "GOOD")
    assert good["avg_r"] == pytest.approx(0.4) and good["profit_factor"] == float("inf")
    assert {b["bucket"] for b in rep["score_buckets"]} == {"40-55", "70-100"}
    edges = edges_from(rows)
    assert edges["BAD:long"] == pytest.approx(-0.5 * 30 / 60)       # shrunk by n/(n+30)


@pytest.mark.rule("TS-12")
def test_walk_forward_grades_every_idea_and_disabled_is_respected() -> None:
    s = {f"S{k}": random_walk(420, seed=k + 30, sym=f"S{k}") for k in range(3)}
    graded = walk_forward(s, None, max_hold=5)
    assert graded and all(x.outcome.sessions <= 5 for x in graded)
    names = {x.idea.technique for x in graded}
    off = walk_forward(s, None, max_hold=5, disabled=frozenset(names))
    assert off == []


# --- TS-13: optional Qwen summary -----------------------------------------------------------


class _Res:
    def __init__(self, text: str | None) -> None:
        self.ok = text is not None
        self.value = None if text is None else type("V", (), {"summary": text})()


class _FakeLLM:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def structured(self, **kw: Any) -> _Res:
        self.calls.append(kw)
        return _Res(self.text)


@pytest.mark.rule("TS-13")
async def test_explain_keeps_only_numbers_from_the_data() -> None:
    s = pullback_series()
    ideas, _ = scan({"TST": s}, None)
    d = ideas[0]
    good = _FakeLLM(f"TST {d.technique}: buy-stop {d.trigger:.2f}, stop {d.stop:.2f}.")
    assert await explain(good, ideas, []) == good.text
    assert good.calls[0]["agent"] == "scan" and "swing" in good.calls[0]["data"]
    assert await explain(_FakeLLM("TST target 999.99 next week."), ideas, []) is None
    assert await explain(_FakeLLM(None), ideas, []) is None
    assert await explain(_FakeLLM("x"), [], []) is None
    assert numbers_ok("range 3.3%", {"a": 3.26}) and not numbers_ok("3.4", {"a": 3.26})


# --- TS-14..TS-16: CLI, config, data ---------------------------------------------------------


def _write_store(root: Path, series: dict[str, Series]) -> None:
    from trader.data.store import BarStore
    from trader.models.messages import Bar

    store = BarStore(root)
    for sym, s in series.items():
        rows = [Bar(symbol=sym, ts=datetime(d.year, d.month, d.day, 4, tzinfo=UTC),
                    timeframe="1Day", o=float(o), h=float(h), l=float(lo), c=float(c),
                    v=int(v))
                for d, o, h, lo, c, v in zip(s.dates, s.o, s.h, s.l, s.c, s.v, strict=True)]
        store.write("daily", sym, rows)


@pytest.mark.rule("TS-14")
@pytest.mark.rule("TS-16")
def test_cli_scan_and_scan_eval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:
    from trader.cli import main

    data = {"AAA": random_walk(420, seed=41, sym="AAA"),
            "BBB": random_walk(420, seed=42, sym="BBB"),
            "CCC": random_walk(100, seed=43, sym="CCC"),
            "SPY": random_walk(420, seed=44, sym="SPY", drift=0.0006)}
    _write_store(tmp_path / "bars", data)
    eval_file = tmp_path / "eval.json"
    cfg = tmp_path / "config.yaml"
    macro = Path(__file__).resolve().parents[2] / "config" / "macro_calendar_2026_2027.yaml"
    cfg.write_text("universe:\n  tickers: [AAA, BBB, CCC]\n"
                   f"windows:\n  macro_calendar_file: {macro.as_posix()}\n"
                   f"scanner:\n  eval_file: {eval_file.as_posix()}\n  min_score: 0\n",
                   encoding="utf-8")
    monkeypatch.setenv("TRADER_BARS_PATH", str(tmp_path / "bars"))

    assert main(["--config", str(cfg), "scan-eval"]) == 0
    out = capsys.readouterr().out
    assert "technique" in out and "Day watchlist" in out and eval_file.exists()
    edges, label = load_edges(str(eval_file))
    assert edges is not None and label

    assert main(["--config", str(cfg), "scan", "--json"]) == 0
    js = json.loads(capsys.readouterr().out)
    assert {p["symbol"] for p in js["in_play"]} == {"AAA", "BBB"}
    assert "CCC" in js["skipped"] and "history" in js["skipped"]["CCC"]       # TS-16
    for d in js["swing"]:
        assert d["side"] in ("long", "short") and d["trigger"] and d["stop"]

    assert main(["--config", str(cfg), "scan"]) == 0
    text = capsys.readouterr().out
    assert "SWING IDEAS" in text and "DAY WATCHLIST" in text and "eval.json" in text

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main(["--config", str(cfg), "scan", "--telegram"]) == 1           # no token: says so


@pytest.mark.rule("TS-15")
def test_scanner_config_defaults_and_validation() -> None:
    base = {"universe": {"tickers": ["AAA"]}}
    cfg = config_mod.Config.model_validate(base)
    assert cfg.scanner.min_score == 50 and "REVERSAL_CANDLE:long" in cfg.scanner.disabled
    assert not [p for p in config_mod.validate(cfg) if p.startswith("scanner")]
    bad = config_mod.Config.model_validate(
        {**base, "scanner": {"disabled": ["NOPE", "BULL_FLAG:up"], "min_score": 120}})
    problems = [p for p in config_mod.validate(bad) if p.startswith("scanner")]
    assert len(problems) == 3
    ok = config_mod.Config.model_validate(
        {**base, "scanner": {"disabled": ["BEAR_FLAG", "BREAKOUT_20D:long"], "symbols": ["amd"]}})
    assert ok.scanner.symbols == ("AMD",)
    assert not [p for p in config_mod.validate(ok) if p.startswith("scanner")]


@pytest.mark.rule("TS-15")
def test_disabled_techniques_by_name_or_side() -> None:
    s = pullback_series()
    i = len(s.c) - 1
    fr = T.frames(s, None)
    names = {d.technique for d in ideas_at(fr, i, dedup=False)}
    assert "TREND_PULLBACK" in names
    for off in ("TREND_PULLBACK", "TREND_PULLBACK:long"):
        left = {d.technique for d in ideas_at(fr, i, frozenset({off}), dedup=False)}
        assert "TREND_PULLBACK" not in left
    left = ideas_at(fr, i, frozenset({"TREND_PULLBACK:short"}), dedup=False)
    assert "TREND_PULLBACK" in {d.technique for d in left}


@pytest.mark.rule("TS-16")
def test_short_history_is_skipped_not_guessed() -> None:
    ideas, skipped = scan({"NEW": random_walk(150, sym="NEW")}, None)
    assert ideas == [] and "150 sessions" in skipped["NEW"]
    s = random_walk(300, sym="OLD")
    _, skipped = scan({"OLD": s}, None, asof=s.dates[-1] + timedelta(days=1))
    assert "no bar for" in skipped["OLD"]
