"""Momentum A19, MO-1..MO-25 (MOMENTUM_REQUIREMENTS.md), on synthetic bars."""

from __future__ import annotations

import asyncio
import json
import math
import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trader.config import Config, validate
from trader.journal.store import SqlJournal
from trader.momentum import factors as fx
from trader.momentum import rotation
from trader.momentum.agent import (
    MomentumAgent,
    alert_key,
    refresh_loader,
    render,
    render_alert,
    setup_record,
    store_loader,
)
from trader.momentum.evaluate import (
    Trade,
    build_panel,
    evaluate,
    gate,
    portfolio,
    rotation_grid,
    signals,
    stats,
)
from trader.momentum.exits import POLICIES, ExitParams, Position, enter, simulate, step
from trader.momentum.factors import MomFrame, Panel, regime
from trader.momentum.report import eval_text
from trader.momentum.setups import SETUP_NAMES, SETUPS, setups_at
from trader.scanner.techniques import Series
from trader.timeutil import et_datetime

ROOT = Path(__file__).resolve().parents[2]
D0 = date(2022, 1, 3)


def sessions(n: int, start: date = D0) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def walk(sym: str, n: int, seed: int, drift: float = 0.0, vol: float = 0.012,
         price: float = 100.0, volume: float = 2e6) -> Series:
    rng = random.Random(seed)
    c, rows = price, []
    for d in sessions(n):
        o = c * (1 + rng.gauss(0, vol / 3))
        c = c * (1 + rng.gauss(drift, vol))
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        rows.append((d, o, hi, lo, c, volume * (1 + rng.random())))
    return Series.from_rows(sym, rows)


def from_closes(sym: str, closes: Sequence[float], *, vols: Sequence[float] | None = None,
                opens: Sequence[float] | None = None, spread: float = 0.006,
                start: date = D0) -> Series:
    rows = []
    for k, (d, c) in enumerate(zip(sessions(len(closes), start), closes, strict=True)):
        o = opens[k] if opens is not None else (closes[k - 1] if k else c)
        rows.append((d, o, max(o, c) * (1 + spread), min(o, c) * (1 - spread), c,
                     (vols[k] if vols is not None else 2e6)))
    return Series.from_rows(sym, rows)


def uptrend(n: int, g: float = 0.0015, start: float = 50.0) -> list[float]:
    return [start * (1 + g) ** k for k in range(n)]


class Bars:
    """Hand-set arrays for the exit state machine (duck-typed MomFrame)."""

    def __init__(self, ohlc: Sequence[tuple[float, float, float, float]], atr: float = 5.0,
                 ema: float = 0.0, sma50: float | None = None) -> None:
        n = len(ohlc)
        a = np.asarray(ohlc, dtype=np.float64)
        self.o, self.h, self.l, self.c = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
        self.atr = np.full(n, atr)
        self.atr22 = np.full(n, atr)
        self.ema10 = np.full(n, ema)
        self.ema20 = np.full(n, ema)
        self.sma50 = np.full(n, sma50 if sma50 is not None else np.nan)
        self.dates = sessions(n)
        self.pos = {d: i for i, d in enumerate(self.dates)}


P0 = ExitParams(policy="ema20", cost_bps=0.0)


# --- MO-2..MO-6 factors, rating, template, regime -------------------------------------------

@pytest.mark.rule("MO-2")
def test_factor_math() -> None:
    c = np.asarray(uptrend(300, 0.001, 100.0))
    r = fx.ret(c, 252, 21)
    assert r[299] == pytest.approx(c[278] / c[47] - 1)
    assert math.isnan(r[251])
    cl = fx.clenow(c, 90)
    assert cl[299] == pytest.approx(1.001 ** 252 - 1, rel=1e-6)      # R-squared = 1
    noisy = np.asarray([100 * (1.001 ** k) * (1 + 0.03 * (-1) ** k) for k in range(300)])
    assert fx.clenow(noisy, 90)[299] < cl[299]
    rng = random.Random(1)
    m = np.cumprod([1 + rng.gauss(0, 0.01) for _ in range(300)]) * 100
    idio = np.cumprod([1 + rng.gauss(0, 0.004) for _ in range(300)])
    beta_only = m * idio
    alpha = m * idio * np.cumprod([1.003] * 300)
    rm_beta = fx.residual_momentum(beta_only, m)[299]
    rm_alpha = fx.residual_momentum(alpha, m)[299]
    assert rm_alpha > 5 and abs(rm_beta) < 3          # a z-score: pure beta is noise
    smooth = np.asarray(uptrend(300, 0.001))
    jumpy = np.asarray([100.0 * (1.3 if k > 200 else 1.0) * (1 - 0.0002 * k) for k in range(300)])
    assert fx.info_discreteness(smooth)[299] < 0 < fx.info_discreteness(jumpy)[299] + 1
    h, lo = c * 1.02, c * 0.98
    assert fx.adr_pct(h, lo)[299] == pytest.approx((1.02 / 0.98 - 1) * 100)
    o = c.copy()
    o[280] = c[279] * 1.2
    assert fx.max_gap(o, c)[299] == pytest.approx(0.2, abs=1e-9)


def universe(n_sym: int = 12, n: int = 330, seed: int = 0) -> tuple[dict[str, Series], Series]:
    series = {f"S{k:02d}": walk(f"S{k:02d}", n, seed + k, drift=0.0004 * (k - 4), vol=0.01)
              for k in range(n_sym)}
    return series, walk("SPY", n, 99, drift=0.0004, vol=0.008)


@pytest.mark.rule("MO-3")
@pytest.mark.rule("MO-5")
def test_rs_rating_and_leaderboard() -> None:
    series, spy = universe()
    panel = build_panel(series, spy, rank_factors=("r12_1",))
    d = spy.dates[-1]
    rs = panel.rs(d)
    assert set(rs.values()) <= set(range(1, 100)) and max(rs.values()) == 99
    assert min(rs.values()) == 1
    board = panel.leaderboard(d, 5)
    assert [r.rank for r in board] == [1, 2, 3, 4, 5]
    comp = [r.composite for r in board]
    assert comp == sorted(comp, reverse=True)
    r12 = {k: float(panel.frames[k].f["r12_1"][panel.frames[k].pos[d]]) for k in rs}
    assert board[0].symbol == max(r12, key=lambda k: r12[k])
    assert {"r12_1", "clenow", "high52"} <= set(board[0].factors)


@pytest.mark.rule("MO-1")
@pytest.mark.rule("MO-5")
def test_liquidity_filter_and_min_history() -> None:
    series, spy = universe(4)
    series["PENNY"] = walk("PENNY", 330, 7, price=5.0)                 # under $10
    series["THIN"] = walk("THIN", 330, 8, volume=1e4)                   # under $50M a day
    series["NEW"] = walk("NEW", 200, 9)                                 # under 260 sessions
    panel = build_panel(series, spy)
    d = spy.dates[-1]
    el = panel.eligible(d)
    assert "PENNY" not in el and "THIN" not in el and "NEW" not in el
    assert len(el) == 4


@pytest.mark.rule("MO-4")
def test_trend_template_and_stage() -> None:
    up = MomFrame(from_closes("UP", uptrend(320)), None)
    score, failed = up.template(319, 90)
    assert score == 8 and failed == []
    assert up.stage(319) == 2
    down = MomFrame(from_closes("DN", list(reversed(uptrend(320)))), None)
    score, failed = down.template(319, 10)
    assert score <= 1 and "close>SMA50" in failed and "RS>=70" in failed
    assert down.stage(319) == 4


@pytest.mark.rule("MO-6")
def test_regime_states() -> None:
    up = from_closes("SPY", uptrend(300, 0.001))
    assert regime(up, up.dates[-1]).state == "ON"
    assert regime(up, up.dates[-1], "RISK_OFF").state == "CAUTION"
    down = from_closes("SPY", list(reversed(uptrend(300, 0.001))))
    r = regime(down, down.dates[-1])
    assert r.state == "OFF" and r.size_mult == 0
    # a bear market (-30% over a year) with a sharp rebound that lifts SPY over its SMA200
    bear = [100 * (1 - 0.0016) ** k for k in range(280)]
    rebound = [bear[-1] * (1.009 ** k) for k in range(1, 25)]
    crash = from_closes("SPY", bear + rebound)
    st = regime(crash, crash.dates[-1])
    assert st.state in ("CAUTION", "OFF")
    if st.state == "CAUTION":
        assert any("crash guard" in x for x in st.reasons) and st.size_mult == 0.5
    assert regime(None, D0).state == "UNKNOWN"


# --- MO-7 setups ----------------------------------------------------------------------------

def _frame(closes: list[float], vols: list[float] | None = None,
           opens: list[float] | None = None, spread: float = 0.006) -> MomFrame:
    return MomFrame(from_closes("X", closes, vols=vols, opens=opens, spread=spread), None)


@pytest.mark.rule("MO-7")
def test_donchian_and_high52w_setups() -> None:
    from trader.momentum.setups import donchian_55, high_52w

    x = _frame(uptrend(300))
    c = donchian_55(x, 299)
    assert c is not None
    lvl = float(x.h[245:300].max())
    assert c.trigger == pytest.approx(lvl + 0.05 * x.atr[299])
    assert c.stop == pytest.approx(c.trigger - 2 * x.atr20[299])
    closes = uptrend(290) + [uptrend(290)[-1] * (1 - 0.0002 * k) for k in range(12)]
    y = _frame(closes, spread=0.002)
    i = len(closes) - 1
    hi = high_52w(y, i)
    assert hi is not None and hi.trigger > y.c[i]


@pytest.mark.rule("MO-7")
def test_episodic_pivot_and_pocket_pivot() -> None:
    from trader.momentum.setups import episodic_pivot, pocket_pivot

    closes = uptrend(120, 0.001)
    opens = list(closes)
    vols = [1e6] * 120
    closes[-1] = closes[-2] * 1.14
    opens[-1] = closes[-2] * 1.10
    vols[-1] = 5e6
    ep = episodic_pivot(_frame(closes, vols, opens, spread=0.004), 119)
    assert ep is not None and "gap +10%" in ep.why
    vols[-1] = 2e6                                         # not enough volume
    assert episodic_pivot(_frame(closes, vols, opens), 119) is None
    # pocket pivot: an uptrend that pauses (down days on 1M) then an up day on 1.5M
    base = uptrend(100, 0.002)
    tail = [base[-1] * (1 + 0.002 * ((-1) ** k)) for k in range(10)] + [base[-1] * 1.004]
    pv = [1e6] * 110 + [1.5e6]
    x = _frame(base + tail, pv)
    pp = pocket_pivot(x, 110)
    assert pp is not None and pp.entry == "open"


@pytest.mark.rule("MO-7")
def test_vcp_breakout_and_high_tight_flag() -> None:
    from trader.momentum.setups import high_tight_flag, momentum_breakout, vcp

    run = uptrend(150, 0.004, 50.0)
    top = run[-1]
    wide = [top * (0.9 + 0.08 * ((k // 3) % 2)) for k in range(40)]     # 20% base
    tight = [top * (0.985 + 0.01 * (k % 2)) for k in range(10)]         # last 10: 2%
    closes = run + wide + tight
    vols = [2e6] * 190 + [1e6] * 10
    x = _frame(closes, vols, spread=0.003)
    i = len(closes) - 1
    v = vcp(x, i)
    assert v is not None and v.trigger > x.c[i] and "base" in v.why
    # momentum breakout: +40% in 50 sessions, then a tightening 12-session base near the high
    pre = uptrend(80, 0.0005, 50.0) + uptrend(50, 0.0068, uptrend(80, 0.0005, 50.0)[-1])
    hi = pre[-1]
    base = [hi * (0.94 + 0.04 * (k % 2)) for k in range(6)] + \
        [hi * (0.975 + 0.004 * (k % 2)) for k in range(6)]
    y = _frame(pre + base, spread=0.002)
    j = len(pre + base) - 1
    mb = momentum_breakout(y, j)
    assert mb is not None and "advance" in mb.why
    # high tight flag: doubles in 30 sessions, then a 15-session flag down 12%
    flat = [40.0] * 80
    pole = [40.0 * (2.2 ** (k / 30)) for k in range(1, 31)]
    flag = [pole[-1] * (0.9 + 0.02 * (k % 3)) for k in range(15)]
    z = _frame(flat + pole + flag, spread=0.004)
    htf = high_tight_flag(z, len(flat + pole + flag) - 1)
    assert htf is not None and "flag" in htf.why


@pytest.mark.rule("MO-7")
def test_setups_need_a_leader_and_an_r_inside_the_window() -> None:
    x = _frame(uptrend(300))
    names = {st.name for st, _ in setups_at(x, 299, leader=True)}
    assert "DONCHIAN_55" in names
    assert "DONCHIAN_55" not in {st.name for st, _ in setups_at(x, 299, leader=False)}
    assert not setups_at(x, 299, True, frozenset({"DONCHIAN_55", "HIGH_52W", "VCP",
                                                  "MOMENTUM_BREAKOUT", "POCKET_PIVOT"}))
    for st, c in setups_at(x, 299, leader=True):
        r = c.trigger - c.stop
        assert 0.25 * x.atr[299] <= r <= st.r_max_atr * x.atr[299]
    assert len(SETUPS) == 8 and len(set(SETUP_NAMES)) == 8


# --- MO-8 / MO-9 entries and exits ----------------------------------------------------------

@pytest.mark.rule("MO-8")
def test_entry_rules() -> None:
    base = (99.0, 99.5, 98.0, 99.0)
    gap = Bars([base, (103.0, 104.0, 102.5, 103.5)])
    p, ev = enter(gap, 1, 100.0, 95.0, "stop", P0)               # opened > 0.5R above
    assert p is None and ev[0].kind == "SKIP" and "not chased" in ev[0].note
    miss = Bars([base, (99.0, 99.9, 98.5, 99.5)])
    p, ev = enter(miss, 1, 100.0, 95.0, "stop", P0)
    assert p is None and ev[0].kind == "EXPIRE"
    ok = Bars([base, (99.5, 101.0, 99.0, 100.5)])
    p, _ = enter(ok, 1, 100.0, 95.0, "stop", P0)
    assert p is not None and p.entry == 100.0 and p.risk == 5.0
    wide = Bars([base, (99.5, 101.0, 99.0, 100.5)], atr=20.0)
    p, _ = enter(wide, 1, 100.0, 80.0, "stop", P0)             # stop raised to the 8% cap
    assert p is not None and p.stop == pytest.approx(92.0)
    opn = Bars([base, (96.0, 97.0, 95.5, 96.5)])
    p, ev = enter(opn, 1, 99.0, 95.0, "open", P0)              # R 1.0 < 0.25 ATR (1.25)
    assert p is None and "opened at the stop" in ev[0].note
    p, _ = enter(Bars([base, (97.0, 98.0, 96.5, 97.5)]), 1, 99.0, 95.0, "open", P0)
    assert p is not None and p.entry == 97.0


@pytest.mark.rule("MO-9")
def test_stop_partial_breakeven_and_trend_exit() -> None:
    bars = [(99.0, 99.5, 98.0, 99.0), (99.5, 101.0, 99.0, 100.5),
            (101.0, 111.0, 100.5, 110.0), (109.5, 110.0, 107.5, 108.0),
            (107.0, 107.5, 106.0, 106.5)]
    x = Bars(bars)
    x.ema20[3] = 109.0                                       # the close under EMA20 on bar 3
    p, events = simulate(x, 0, 100.0, 95.0, "stop", P0)
    assert p is not None and p.closed
    kinds = [e.kind for e in events]
    assert kinds[:2] == ["ENTER", "PARTIAL"] and "RAISE_STOP" in kinds
    trail = [e for e in events if e.kind == "TRAIL"]
    assert trail[0].at_open_next and not trail[0].executed
    assert trail[1].executed and trail[1].price == 107.0
    assert p.realized_r == pytest.approx(1 / 3 * 2 + 2 / 3 * 7 / 5)
    stop = Bars([(99.0, 99.5, 98.0, 99.0), (99.5, 101.0, 99.0, 100.5),
                 (100.0, 100.5, 94.0, 95.5)])
    p, _ = simulate(stop, 0, 100.0, 95.0, "stop", P0)
    assert p is not None and p.exit_reason == "STOP" and p.realized_r == pytest.approx(-1)
    gapped = Bars([(99.0, 99.5, 98.0, 99.0), (99.5, 101.0, 99.0, 100.5),
                   (90.0, 91.0, 89.0, 90.5)])
    p, ev = simulate(gapped, 0, 100.0, 95.0, "stop", P0)
    assert p is not None and p.realized_r == pytest.approx(-2.0) and "gapped" in ev[-1].note
    cost = ExitParams(policy="ema20", cost_bps=10.0)
    p2, _ = simulate(stop, 0, 100.0, 95.0, "stop", cost)
    assert p2 is not None and p2.realized_r < -1


@pytest.mark.rule("MO-9")
def test_day_partial_climax_and_time_stop() -> None:
    rise = [(99.0, 99.5, 98.0, 99.0), (99.5, 101.0, 99.0, 100.5)] + \
        [(100.5 + k, 102 + k, 100 + k, 101.5 + k) for k in range(6)]
    x = Bars(rise)
    p, ev = simulate(x, 0, 100.0, 95.0, "stop", P0)
    part = [e for e in ev if e.kind == "PARTIAL"]
    assert part[0].at_open_next and part[1].executed            # decided on day 5, sold next
    assert p is not None and p.partial_done and p.stop == pytest.approx(100.0)
    clim = Bars(rise, sma50=0.0)  # set per bar below
    clim.sma50[:] = clim.c - 1.0
    clim.sma50[3] = clim.c[3] - 40.0                             # 8 ATR above SMA50
    p, ev = simulate(clim, 0, 100.0, 95.0, "stop", P0)
    c = [e for e in ev if e.kind == "CLIMAX"]
    assert c[0].at_open_next and c[1].executed and c[1].frac == pytest.approx(0.5)
    assert p is not None and p.policy == "ema10"
    flat = Bars([(99.0, 99.5, 98.0, 99.0), (99.5, 100.6, 99.0, 99.8)] +
                [(99.8, 100.4, 99.2, 99.9)] * 12)
    p, ev = simulate(flat, 0, 100.0, 95.0, "stop", P0)
    assert p is not None and p.exit_reason == "TIME"
    t = [e for e in ev if e.kind == "TIME"]
    assert t[0].index == 11 and t[1].index == 12              # 10 sessions held, then the open


@pytest.mark.rule("MO-9")
@pytest.mark.rule("MO-10")
def test_trailing_stops_only_ratchet_up() -> None:
    bars = [(99.0, 99.5, 98.0, 99.0), (99.5, 101.0, 99.0, 100.5)] + \
        [(101 + k, 103 + k, 100.5 + k, 102 + k) for k in range(5)] + \
        [(105.0, 105.5, 103.5, 104.0)] * 3
    x = Bars(bars, atr=1.0)
    prm = ExitParams(policy="chandelier", cost_bps=0.0, partial_r=50)
    p, _ = enter(x, 1, 100.0, 95.0, "stop", prm)
    assert p is not None
    stops = []
    for k in range(2, len(bars)):
        step(x, k, p, prm)
        stops.append(p.stop)
    assert stops == sorted(stops)                              # never lowered
    assert stops[-1] == pytest.approx(max(b[1] for b in bars[1:]) - 3.0)
    assert set(POLICIES) == {"ema10", "ema20", "sma50", "chandelier", "donchian20"}


@pytest.mark.rule("MO-9")
def test_entry_day_stop_rule_close_versus_low() -> None:
    x = Bars([(99.0, 99.5, 98.0, 99.0), (99.0, 101.0, 94.0, 100.0)])
    p, _ = enter(x, 1, 100.0, 95.0, "stop", P0)
    assert p is not None and not p.closed                      # low likely before the fill
    low = ExitParams(policy="ema20", cost_bps=0.0, entry_day_stop="low")
    p, ev = enter(x, 1, 100.0, 95.0, "stop", low)
    assert p is not None and p.closed and ev[-1].kind == "STOP"


@pytest.mark.rule("MO-15")
def test_position_state_round_trips() -> None:
    x = Bars([(99.0, 99.5, 98.0, 99.0), (99.5, 101.0, 99.0, 100.5),
              (101.0, 111.0, 100.5, 110.0)])
    p, _ = enter(x, 1, 100.0, 95.0, "stop", P0)
    assert p is not None
    q = Position.from_dict(json.loads(json.dumps(p.as_dict())))
    step(x, 2, p, P0)
    step(x, 2, q, P0)
    assert p.as_dict() == q.as_dict()
    assert not step(x, 2, p, P0)                               # a session is applied once


# --- MO-19 no look-ahead ---------------------------------------------------------------------

@pytest.mark.rule("MO-19")
def test_no_look_ahead_in_factors_ratings_and_setups() -> None:
    series, spy = universe(8, 340)
    full = build_panel(series, spy)
    cut_at = 300
    cut = {k: v.upto(cut_at) for k, v in series.items()}
    part = build_panel(cut, spy.upto(cut_at))
    d = spy.dates[cut_at - 1]
    assert full.rs(d) == part.rs(d)
    assert full.composite(d) == pytest.approx(part.composite(d))
    for k in series:
        a, b = full.frames[k], part.frames[k]
        i = cut_at - 1
        for f in fx.FACTORS:
            va, vb = a.f[f][i], b.f[f][i]
            assert (math.isnan(va) and math.isnan(vb)) or va == pytest.approx(vb)
        sa = [(s.name, c.trigger) for s, c in setups_at(a, i, True)]
        sb = [(s.name, c.trigger) for s, c in setups_at(b, i, True)]
        assert sa == sb


# --- MO-11..MO-14 evaluation ------------------------------------------------------------------

def _trade(sym: str, d: date, r: float) -> Trade:
    return Trade(sym, "DONCHIAN_55", "donchian20", d, d, d, 100.0, 5.0, r, True, 5, 90, 8,
                 True, "ON", "STOP")


@pytest.mark.rule("MO-11")
@pytest.mark.rule("MO-14")
def test_stats_halves_and_gate() -> None:
    days = sessions(80)
    trs = [_trade("A", days[k], 1.5 if k % 2 else -1.0) for k in range(80)]
    st = stats(trs)
    assert st["trades"] == 80 and st["win_rate"] == 0.5
    assert st["avg_r"] == pytest.approx(0.25) and st["profit_factor"] == pytest.approx(1.5)
    assert st["ci95"] is not None and st["max_consecutive_losses"] == 1
    h = {"is": stats(trs[:40]), "oos": stats(trs[40:]), "all": st}
    assert gate(h)["pass"]
    bad = {"is": stats(trs[:40]), "oos": stats([_trade("A", days[0], -1.0)] * 40), "all": st}
    g = gate(bad)
    assert not g["pass"] and not g["checks"]["avg_r_gt_0_out_of_sample"]
    assert not gate(h, min_trades=100)["pass"]


@pytest.mark.rule("MO-11")
@pytest.mark.rule("MO-12")
@pytest.mark.rule("MO-13")
@pytest.mark.rule("MO-24")
def test_walk_forward_evaluation_end_to_end() -> None:
    series, spy = universe(12, 420, seed=3)
    res = evaluate(series, spy, params=ExitParams(cost_bps=10.0),
                   setup_policy={}, min_rs=50, min_template=5)
    assert set(res["setups"]) == set(SETUP_NAMES)
    for r in res["setups"].values():
        assert set(r["policies"]) == set(POLICIES)
        assert {"is", "oos", "all"} <= set(r["policies"][r["default_policy"]])
        assert "no_regime_filter" in r["ablation"] and "pessimistic_entry_day" in r["ablation"]
    assert {"gated_setups", "all_setups"} == set(res["alert_stream"])
    assert {"spy", "equal_weight"} == set(res["benchmarks"])
    rot = res["rotation"]
    assert rot["selected"]["name"] in rot["runs"] and "pass" in rot["gate"]
    assert "survivorship" in res["survivorship_note"]
    text = eval_text(res)
    assert "Rotation model" in text and "selected on the in-sample half" in text


@pytest.mark.rule("MO-12")
@pytest.mark.rule("MO-17")
def test_portfolio_slots_heat_and_no_leverage() -> None:
    series, spy = universe(6, 330, seed=4)
    panel = build_panel(series, spy)
    days = spy.dates[-40:]
    syms = sorted(panel.frames)
    trs = []
    for k, sym in enumerate(syms):
        x = panel.frames[sym]
        i = x.pos[days[2]]
        t = Trade(sym, "DONCHIAN_55", "ema20", days[1], days[2], days[10], float(x.c[i]),
                  float(x.c[i]) * 0.05, 0.0, True, 8, 99 - k, 8, True, "ON", "TRAIL")
        from trader.momentum.exits import Event
        t.events = [Event("ENTER", i, float(x.c[i])),
                    Event("TRAIL", x.pos[days[10]], float(x.c[x.pos[days[10]]]), frac=1.0)]
        trs.append(t)
    pf = portfolio(trs, panel, days, risk_pct=1.0, max_positions=3, max_heat_pct=6.0,
                   max_correlated=99)
    assert pf["taken"] == 3 and pf["skipped_for_slots_or_heat"] == 3
    assert pf["avg_exposure"] is not None and pf["avg_exposure"] <= 1.0
    heat = portfolio(trs, panel, days, risk_pct=1.0, max_positions=8, max_heat_pct=2.0,
                     max_correlated=99)
    assert heat["taken"] == 2


@pytest.mark.rule("MO-13")
@pytest.mark.rule("MO-25")
def test_rotation_calendar_targets_and_cap() -> None:
    fri = date(2026, 10, 16)                                    # ISO week 42
    assert rotation.rebalance_due(fri, date(2026, 10, 19), 2)
    assert not rotation.rebalance_due(date(2026, 10, 15), fri, 2)
    assert not rotation.rebalance_due(date(2026, 10, 9), date(2026, 10, 12), 2)  # week 41
    w = rotation.cap_weights({"A": 0.6, "B": 0.2, "C": 0.2}, 0.4)
    assert w["A"] == pytest.approx(0.4) and sum(w.values()) == pytest.approx(1.0)
    w = rotation.cap_weights({"A": 0.9, "B": 0.1}, 0.3)
    assert max(w.values()) <= 0.3 + 1e-9 and sum(w.values()) == pytest.approx(0.6)
    series, spy = universe(12, 330, seed=5)
    panel = build_panel(series, spy, rank_factors=("r6_1",))
    d = spy.dates[-1]
    t = rotation.targets(panel, spy, d, {}, None, 4, 8, False, max_weight=0.3)
    assert 0 < len(t) <= 4 and sum(t.values()) <= 1 + 1e-9 and max(t.values()) <= 0.3 + 1e-9
    for sym in t:
        x = panel.frames[sym]
        assert x.c[x.pos[d]] > x.sma100[x.pos[d]]
    down = from_closes("SPY", list(reversed(uptrend(330, 0.001))))
    down = Series("SPY", spy.dates, down.o, down.h, down.l, down.c, down.v)
    assert rotation.targets(panel, down, d, {}, None, 4, 8, True) == {}   # regime OFF


@pytest.mark.rule("MO-13")
def test_rotation_holds_one_session_lag() -> None:
    series, spy = universe(12, 330, seed=6)
    panel = build_panel(series, spy)
    dates = [d for d in spy.dates if len(panel.eligible(d)) >= 10]
    r = rotation.run(panel, spy, dates, top_n=3, cost_bps=0.0, regime_filter=False)
    first = r.rebalances[0][0]
    k = dates.index(first)
    assert r.equity[k + 1] == pytest.approx(r.equity[k])      # bought on the next close
    assert len(r.equity) == len(dates)


@pytest.mark.rule("MO-13")
@pytest.mark.rule("MO-24")
def test_rotation_choice_uses_the_in_sample_half_only() -> None:
    series, spy = universe(12, 420, seed=7)
    panel = build_panel(series, spy)
    cal = [d for d in spy.dates if len(panel.eligible(d)) >= 10]
    split = cal[len(cal) // 2]
    from trader.momentum.evaluate import _curve_stats, benchmarks
    bench = {k: _curve_stats(cal, v, split) for k, v in benchmarks(panel, spy, cal).items()}
    res = rotation_grid(panel, spy, cal, split, bench, rank_factors=("r12_1", "clenow"),
                        top_n=3, hold_rank=6, weeks=2, cost_bps=0.0)
    best_is = max(res["runs"], key=lambda k: (res["runs"][k].get("is") or {}).get(
        "cagr_pct", -1e9))
    assert res["selected"]["name"] == best_is
    assert len(res["runs"]) == 2 * (len(fx.FACTORS) + 2)


# --- MO-15..MO-18 live tracker and alerts ----------------------------------------------------

def _eval_file(path: Path, proven: Sequence[str]) -> None:
    setups = {}
    for st in SETUPS:
        ok = st.name in proven
        setups[st.name] = {"default_policy": st.policy, "gate": {"pass": ok, "checks": {
            "trades_ge_30": ok}}, "policies": {st.policy: {"all": {
                "trades": 100, "win_rate": 0.5, "avg_r": 0.2, "profit_factor": 1.5}}}}
    path.write_text(json.dumps({"generated": "2026-10-01", "setups": setups,
                                "gate_pass": list(proven), "rotation": {
                                    "selected": {"name": "r6_1", "factors": ["r6_1"],
                                                 "regime_filter": False},
                                    "gate": {"pass": True}, "runs": {}}}), encoding="utf-8")


def _mcfg(cfg: Config, tmp_path: Path, **kw: Any) -> Config:
    mo = cfg.momentum.model_copy(update={
        "eval_file": str(tmp_path / "eval.json"), "latest_file": str(tmp_path / "m.json"),
        "universe": tuple(f"S{k:02d}" for k in range(12)) + ("LEAD",), **kw})
    return cfg.model_copy(update={"momentum": mo})


def _loader(store: dict[str, Series]) -> Any:
    async def load(symbols: Sequence[str], until: date) -> dict[str, Series]:
        out = {}
        for s in symbols:
            if s in store:
                v = store[s]
                n = sum(1 for d in v.dates if d <= until)
                if n:
                    out[s] = v.upto(n)
        return out
    return load


def _extend(s: Series, bars: Sequence[tuple[float, float, float, float]]) -> Series:
    rows = list(zip(s.dates, s.o, s.h, s.l, s.c, s.v, strict=True))
    d = s.dates[-1]
    for o, h, lo, c in bars:
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append((d, o, h, lo, c, 3e6))
    return Series.from_rows(s.symbol, [(r[0], float(r[1]), float(r[2]), float(r[3]),
                                        float(r[4]), float(r[5])) for r in rows])


def _world(n: int = 330) -> dict[str, Series]:
    store, spy = universe(12, n, seed=11)
    store["SPY"] = spy
    store["LEAD"] = from_closes("LEAD", uptrend(n, 0.003, 40.0), spread=0.004)
    return store


@pytest.mark.rule("MO-15")
@pytest.mark.rule("MO-16")
@pytest.mark.rule("MO-14")
def _calendar(journal: SqlJournal, d: date) -> None:
    """A fresh Finnhub earnings calendar (MO-18), with no date near the test tickers."""
    from trader.journal import schema as js

    journal.insert(js.fundamentals_daily, {"date": d, "ticker": "ZZZ", "kind": "earnings_calendar",
                                           "json": {"date": "2099-01-02"}})


@pytest.mark.rule("MO-18")
def test_earnings_calendar_fails_closed(cfg: Config, tmp_path: Path, journal: SqlJournal
                                        ) -> None:
    c = _mcfg(cfg, tmp_path)
    _eval_file(Path(c.momentum.eval_file), ["DONCHIAN_55"])
    store = _world()
    d0 = store["LEAD"].dates[-1]
    agent = MomentumAgent(c, journal, _loader(store), lambda: et_datetime(d0, time(17, 0)))
    pay = asyncio.run(agent.run(asof=d0, track=False))       # no calendar row at all
    don = next(x for x in pay["candidates"] if x["setup"] == "DONCHIAN_55")
    assert pay["earnings_status"] == "stale" and don["verdict"] == "earnings_unknown"
    assert "held back" in render(pay)
    _calendar(journal, d0 - timedelta(days=c.momentum.earnings_max_age_days + 1))
    assert agent.earnings_status(d0) == "stale"                  # too old
    _calendar(journal, d0 - timedelta(days=1))
    pay = asyncio.run(agent.run(asof=d0, track=False))
    assert pay["earnings_status"] == "fresh"
    assert next(x for x in pay["candidates"] if x["setup"] == "DONCHIAN_55")["verdict"] == "alert"
    bare = MomentumAgent(c, None, _loader(store), lambda: et_datetime(d0, time(17, 0)))
    pay = asyncio.run(bare.run(asof=d0, track=False))
    assert pay["earnings_status"] == "unchecked" and "NOT checked" in render(pay)


def test_tracker_arms_fills_stops_and_alerts_once(cfg: Config, tmp_path: Path,
                                                  journal: SqlJournal) -> None:
    c = _mcfg(cfg, tmp_path)
    _eval_file(Path(c.momentum.eval_file), ["DONCHIAN_55"])
    store = _world()
    d0 = store["LEAD"].dates[-1]
    now = [et_datetime(d0, time(17, 0))]
    agent = MomentumAgent(c, journal, _loader(store), lambda: now[0], equity=100_000.0)
    _calendar(journal, d0)
    pay = asyncio.run(agent.run(asof=d0))
    lead = [x for x in pay["candidates"] if x["ticker"] == "LEAD"]
    alert = [x for x in lead if x["verdict"] == "alert"]
    assert alert and alert[0]["setup"] == "DONCHIAN_55"
    assert all(x["verdict"] in ("busy", "unproven") for x in lead if x is not alert[0])
    trig, stop = alert[0]["trigger"], alert[0]["stop"]
    armed = pay["tracked"]["armed"]
    assert any(a["ticker"] == "LEAD" and a["alerted"] for a in armed)
    text = render(pay)
    assert f"buy-stop {trig}" in text and f"stop {stop}" in text and "trail donchian20" in text
    assert "Record: 100 trades" in text
    # next session: the trigger trades, then a session under the stop
    store["LEAD"] = _extend(store["LEAD"], [(trig - 0.1, trig + 1.0, trig - 0.5, trig + 0.5),
                                            (stop + 0.2, stop + 0.4, stop - 1.0, stop - 0.5)])
    d1, d2 = store["LEAD"].dates[-2], store["LEAD"].dates[-1]
    for k in store:
        if k != "LEAD":
            store[k] = _extend(store[k], [(store[k].c[-1],) * 4] * 2)
    now[0] = et_datetime(d1, time(17, 0))
    p1 = asyncio.run(agent.run(asof=d1))
    ev = [e for e in p1["tracked"]["events"] if e["ticker"] == "LEAD"]
    assert [e["kind"] for e in ev][:1] == ["ENTER"]
    assert any(o["ticker"] == "LEAD" for o in p1["tracked"]["open"])
    again = asyncio.run(agent.run(asof=d1))                      # a re-run sends nothing new
    assert not [e for e in again["tracked"]["events"] if e["ticker"] == "LEAD"]
    now[0] = et_datetime(d2, time(17, 0))
    p2 = asyncio.run(agent.run(asof=d2))
    ev = [e for e in p2["tracked"]["events"] if e["ticker"] == "LEAD"]
    assert [e["kind"] for e in ev] == ["STOP"]
    closed = [x for x in p2["tracked"]["closed"] if x["ticker"] == "LEAD"]
    assert closed and closed[0]["state"] == "CLOSED" and closed[0]["r"] < 0
    assert "SELL ALL (stop)" in render_alert(ev[0])


class FakeRest:
    def __init__(self, snaps: dict[str, dict[str, Any]]) -> None:
        self.snaps = snaps

    async def snapshots(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        return {s: self.snaps[s] for s in symbols if s in self.snaps}


@pytest.mark.rule("MO-15")
def test_intraday_trigger_alert_is_not_repeated_by_the_close_job(
        cfg: Config, tmp_path: Path, journal: SqlJournal) -> None:
    c = _mcfg(cfg, tmp_path)
    _eval_file(Path(c.momentum.eval_file), ["DONCHIAN_55"])
    store = _world()
    d0 = store["LEAD"].dates[-1]
    now = [et_datetime(d0, time(17, 0))]
    agent = MomentumAgent(c, journal, _loader(store), lambda: now[0])
    _calendar(journal, d0)
    pay = asyncio.run(agent.run(asof=d0))
    trig = next(x["trigger"] for x in pay["candidates"] if x["verdict"] == "alert")
    store["LEAD"] = _extend(store["LEAD"], [(trig - 0.1, trig + 1.0, trig - 0.5, trig + 0.5)])
    for k in store:
        if k != "LEAD":
            store[k] = _extend(store[k], [(store[k].c[-1],) * 4])
    d1 = store["LEAD"].dates[-1]
    now[0] = et_datetime(d1, time(11, 0))
    ts = datetime.combine(d1, time(4, 0), tzinfo=UTC).isoformat().replace("+00:00", "Z")
    rest = FakeRest({"LEAD": {"dailyBar": {"t": ts, "o": trig - 0.1, "h": trig + 0.3,
                                           "l": trig - 0.5, "c": trig + 0.2},
                              "latestTrade": {"p": trig + 0.2}}})
    live = asyncio.run(agent.intraday(rest))
    assert [a["kind"] for a in live] == ["ENTER"] and live[0]["live"]
    assert not asyncio.run(agent.intraday(rest))                # once per session
    now[0] = et_datetime(d1, time(17, 0))
    p1 = asyncio.run(agent.run(asof=d1))
    assert not [e for e in p1["tracked"]["events"] if e["kind"] == "ENTER"]
    assert alert_key("ENTER", d1, 1.0) == alert_key("ENTER", d1, 2.0)
    assert alert_key("RAISE_STOP", d1, 1.0) != alert_key("RAISE_STOP", d1, 2.0)


@pytest.mark.rule("MO-14")
@pytest.mark.rule("MO-18")
@pytest.mark.rule("MO-23")
def test_unproven_setups_and_earnings_are_not_alerted(cfg: Config, tmp_path: Path) -> None:
    c = _mcfg(cfg, tmp_path)
    _eval_file(Path(c.momentum.eval_file), [])
    store = _world()
    d0 = store["LEAD"].dates[-1]
    agent = MomentumAgent(c, None, _loader(store), lambda: et_datetime(d0, time(17, 0)))
    pay = asyncio.run(agent.run(asof=d0, track=False))
    lead = [x for x in pay["candidates"] if x["ticker"] == "LEAD"]
    assert lead and all(x["verdict"] in ("unproven", "busy") for x in lead)
    assert all(x["why"] and isinstance(x["failed"], list) for x in lead)
    assert "BUY-STOPS" not in render(pay)
    assert "BUY-STOPS" in render(pay, show_unproven=True)
    _eval_file(Path(c.momentum.eval_file), ["DONCHIAN_55"])
    agent.earnings_in = lambda tickers, asof: {"LEAD": 2}   # type: ignore[method-assign]
    pay = asyncio.run(agent.run(asof=d0, track=False))
    don = next(x for x in pay["candidates"] if x["ticker"] == "LEAD" and
               x["setup"] == "DONCHIAN_55")
    assert don["verdict"] == "earnings" and "earnings in 2" in don["verdict_why"]
    assert setup_record(None, "VCP")["proven"] is False


@pytest.mark.rule("MO-17")
def test_slots_heat_and_correlation_cap(cfg: Config, tmp_path: Path) -> None:
    c = _mcfg(cfg, tmp_path, max_positions=1)
    store = _world()
    store["TWIN"] = Series("TWIN", store["LEAD"].dates, store["LEAD"].o * 2,
                           store["LEAD"].h * 2, store["LEAD"].l * 2, store["LEAD"].c * 2,
                           store["LEAD"].v)
    d0 = store["LEAD"].dates[-1]
    agent = MomentumAgent(c, None, _loader(store), lambda: et_datetime(d0, time(17, 0)))
    series, spy = asyncio.run(agent.series(d0))
    panel = build_panel({k: v for k, v in store.items() if k != "SPY"}, store["SPY"])

    def cand(t: str, risk: float = 500.0) -> dict[str, Any]:
        return {"ticker": t, "verdict": "alert", "verdict_why": "", "risk_usd": risk}
    picks = agent.select(panel, d0, [cand("LEAD"), cand("S01")], [])
    assert [x[1] for x in picks] == [True, False]
    assert picks[1][0]["verdict"] == "no_slot"
    c2 = _mcfg(cfg, tmp_path, max_correlated=1)
    agent2 = MomentumAgent(c2, None, _loader(store), lambda: et_datetime(d0, time(17, 0)))
    picks = agent2.select(panel, d0, [cand("LEAD"), cand("TWIN")], [])
    assert picks[1][0]["verdict"] == "correlated" and "LEAD" in picks[1][0]["verdict_why"]
    agent2.equity = 10_000.0                                     # 6% heat = $600
    picks = agent2.select(panel, d0, [cand("LEAD", 500.0), cand("S03", 500.0)], [])
    assert picks[1][0]["verdict"] == "no_slot" and "open risk" in picks[1][0]["verdict_why"]
    caution = regime(store["SPY"], d0, "RISK_OFF")
    assert caution.size_mult == 0.5


@pytest.mark.rule("MO-25")
def test_rotation_section_on_a_rebalance_day(cfg: Config, tmp_path: Path) -> None:
    c = _mcfg(cfg, tmp_path, top_n=3, hold_rank=6)
    _eval_file(Path(c.momentum.eval_file), [])
    store = _world(340)
    days = store["SPY"].dates
    due = next(d for k, d in enumerate(days[:-1]) if k > 300 and
               rotation.rebalance_due(d, days[k + 1], 2))
    agent = MomentumAgent(c, None, _loader(store), lambda: et_datetime(due, time(17, 0)),
                          equity=50_000.0)
    pay = asyncio.run(agent.run(asof=due, track=False))
    rot = pay["rotation"]
    assert rot["due"] and rot["proven"] and rot["factors"] == ["r6_1"]
    assert {h["action"] for h in rot["holdings"]} <= {"BUY", "SELL", "HOLD"}
    assert sum(h["weight"] for h in rot["holdings"]) <= 1 + 1e-9
    assert any(h["shares"] for h in rot["holdings"] if h["weight"] > 0)
    assert "Rotation model" in render(pay)


# --- MO-20..MO-22 outputs, config, data ------------------------------------------------------

@pytest.mark.rule("MO-20")
def test_cli_commands_and_safe_command() -> None:
    from trader.cli import build_parser
    from trader.notify.commands import SAFE

    p = build_parser()
    for cmd in ("momentum", "momentum-eval", "momentum-history"):
        assert p.parse_args([cmd]).fn is not None
    assert p.parse_args(["momentum", "--equity", "1000", "--telegram"]).equity == 1000
    assert "MOMENTUM" in SAFE
    run_sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    run_ps = (ROOT / "tools" / "run.ps1").read_text(encoding="utf-8")
    for cmd in ("momentum-eval", "momentum-history"):
        assert cmd in run_sh and cmd in run_ps


@pytest.mark.rule("MO-21")
def test_momentum_config_rules(cfg: Config) -> None:
    assert not [p for p in validate(cfg) if p.startswith("momentum")]
    bad = cfg.model_copy(update={"momentum": cfg.momentum.model_copy(update={
        "rank_factors": ("magic",), "disabled": ("NOPE",), "setup_policy": {"VCP": "x"},
        "partial_frac": 1.5, "min_rs": 0, "max_positions": 0, "risk_pct": 5.0,
        "intraday_every_min": 0, "max_correlated": 0, "rotation_max_weight": 0.0,
        "rotation_factors": ("zzz",)})})
    probs = [p for p in validate(bad) if p.startswith("momentum")]
    assert len(probs) >= 10
    assert len(cfg.momentum.universe) >= 100 and len(set(cfg.momentum.universe)) == len(
        cfg.momentum.universe)


@pytest.mark.rule("MO-22")
def test_jump_guard_and_store_refresh(tmp_path: Path) -> None:
    closes = uptrend(330, 0.001)
    closes[300] = closes[299] * 1.7                               # a corporate-action print
    for k in range(301, 330):
        closes[k] = closes[300] * (1.001 ** (k - 300))
    x = MomFrame(from_closes("J", closes), None)
    assert not x.liquid(320, 10, 0) and x.liquid(290, 10, 0)

    from trader.data.store import BarStore
    from trader.models.messages import Bar

    def bar(d: date, c: float) -> Bar:
        return Bar(symbol="A", ts=datetime.combine(d, time(4, 0), tzinfo=UTC), timeframe="1Day",
                   o=c, h=c, l=c, c=c, v=1000)
    days = sessions(5, date(2026, 10, 12))
    BarStore(tmp_path).write("daily_sip", "A", [bar(d, 10.0) for d in days[:3]])
    calls: list[tuple[str, date, datetime]] = []

    class Hist:
        async def daily_bars_until(self, sym: str, start: date, end: datetime) -> list[Bar]:
            calls.append((sym, start, end))
            return [bar(d, 11.0) for d in days[2:]]
    now = et_datetime(days[-1], time(17, 0))
    load = refresh_loader(tmp_path, Hist(), lambda: now)
    got = asyncio.run(load(["A"], days[-1]))
    assert got["A"].dates[-1] == days[-1] and got["A"].c[-1] == 11.0
    assert calls and calls[0][2] <= now - timedelta(minutes=16)
    again = asyncio.run(store_loader(tmp_path)(["A"], days[-1]))
    assert len(again["A"].c) == 5                                 # written back
    calls.clear()
    asyncio.run(load(["A"], days[-1]))
    assert not calls                                              # fresh: no request


@pytest.mark.rule("MO-22")
def test_final_session_ignores_a_partial_bar(cfg: Config, tmp_path: Path) -> None:
    c = _mcfg(cfg, tmp_path)
    d = date(2026, 10, 15)                                        # a Thursday
    agent = MomentumAgent(c, None, _loader({}), lambda: et_datetime(d, time(11, 0)))
    assert agent.final_session() == date(2026, 10, 14)
    agent.now = lambda: et_datetime(d, time(16, 30))
    assert agent.final_session() == d
    agent.now = lambda: et_datetime(date(2026, 10, 18), time(12, 0))   # Sunday
    assert agent.final_session() == date(2026, 10, 16)


@pytest.mark.rule("MO-24")
def test_requirements_record_the_measured_results() -> None:
    doc = (ROOT / "docs" / "MOMENTUM_REQUIREMENTS.md").read_text(encoding="utf-8")
    assert "## 9. Measured results" in doc and "equal-weight" in doc
    assert "survivorship" in doc.lower() and "out-of-sample" in doc


def test_signals_record_leader_and_regime() -> None:
    series, spy = universe(8, 330, seed=12)
    series["LEAD"] = from_closes("LEAD", uptrend(330, 0.003, 40.0), spread=0.004)
    panel: Panel = build_panel(series, spy)
    sigs = signals(panel, spy, min_rs=80, min_template=7)
    lead = [s for s in sigs if s.symbol == "LEAD"]
    assert lead and all(s.leader for s in lead if s.setup == "DONCHIAN_55")
    assert {s.regime for s in sigs} <= {"ON", "CAUTION", "OFF", "UNKNOWN"}


@pytest.mark.rule("MO-20")
def test_console_page_and_command(cfg: Config, tmp_path: Path) -> None:
    from trader.console import momentum as page
    from trader.console.data import ALLOWED as CONSOLE_SAFE
    from trader.console.render import NAV

    c = _mcfg(cfg, tmp_path)
    _eval_file(Path(c.momentum.eval_file), ["DONCHIAN_55"])
    store = _world()
    d0 = store["LEAD"].dates[-1]
    agent = MomentumAgent(c, None, _loader(store), lambda: et_datetime(d0, time(17, 0)))
    pay = asyncio.run(agent.run(asof=d0, track=False))
    html = page.page(pay, json.loads(Path(c.momentum.eval_file).read_text(encoding="utf-8")),
                     "csrf")
    assert "Buy-stops for the next session" in html and "LEAD" in html and "Leaders" in html
    assert "Walk-forward record" in html
    assert "No momentum run yet" in page.page(None, None, "csrf")
    assert "Not graded yet" in page.page(None, None, "csrf")
    assert "MOMENTUM" in CONSOLE_SAFE
    assert any(href == "/momentum" for _, links in NAV for href, _ in links)


# --- MO-26 / MO-27 the 200-day average ------------------------------------------------------

def _pullback_closes(n_up: int = 320, fall: float = 0.012) -> list[float]:
    """A strong advance, then a steady fall to the rising 200-day average."""
    closes = uptrend(n_up, g=0.003)
    while len(closes) < n_up + 120:
        closes.append(closes[-1] * (1 - fall))
    return closes


@pytest.mark.rule("MO-26")
def test_sma200_pullback_arms_on_the_first_touch_only() -> None:
    from trader.momentum.setups import sma200_pullback, sma200_trend

    x = _frame(_pullback_closes())
    hits = [i for i in range(300, len(x.c)) if sma200_pullback(x, i) is not None]
    assert hits, "the fall reaches the 200-day average"
    i = hits[0]
    s = x.sma200[i]
    c = sma200_pullback(x, i)
    assert c is not None and x.l[i] <= 1.01 * s and x.c[i] >= 0.98 * s
    assert c.trigger == pytest.approx(x.h[i] + 0.05 * x.atr[i])
    assert c.stop == pytest.approx(min(x.l[i], s) - 0.5 * x.atr[i])
    assert all(x.c[i - 40:i] > x.sma200[i - 40:i])
    # once closes have been under the average, a later touch is not a first test
    later = [k for k in hits if k > i + 5]
    assert all(not sma200_trend(x, k) or sma200_pullback(x, k) is None for k in later
               if (x.c[k - 40:k] <= x.sma200[k - 40:k]).any())
    # a stock that never ran 15% above its average is not a leader pulling back
    flat = _frame(uptrend(320, g=0.0004) + [uptrend(320, g=0.0004)[-1] * 0.995 ** k
                                            for k in range(1, 60)])
    assert all(sma200_pullback(flat, k) is None for k in range(300, len(flat.c)))


@pytest.mark.rule("MO-26")
def test_sma200_pullback_setup_is_registered_without_the_leader_filter() -> None:
    st = {s.name: s for s in SETUPS}["SMA200_PULLBACK"]
    assert st.needs_leader is False and st.policy == "chandelier"
    x = _frame(_pullback_closes())
    i = next(k for k in range(300, len(x.c)) if st.fn(x, k) is not None)
    assert "SMA200_PULLBACK" in [s.name for s, _ in setups_at(x, i, leader=False)]


@pytest.mark.rule("MO-27")
def test_sma200_watchlist_and_history() -> None:
    from trader.momentum import sma200

    closes = _pullback_closes()
    x = _frame(closes)
    touch = next(k for k in range(300, len(x.c)) if x.l[k] <= 1.01 * x.sma200[k])
    near = next(k for k in range(touch - 1, 300, -1)
                if 0 < x.c[k] / x.sma200[k] - 1 <= 0.06)
    s = from_closes("X", closes)
    spy = from_closes("SPY", uptrend(len(closes)))
    panel = Panel({"X": MomFrame(s, spy)}, min_dollar=0)
    day = s.dates[near]
    rows = sma200.watch(panel, day, always=("X",))
    assert [r["ticker"] for r in rows] == ["X"]
    r = rows[0]
    assert 0 < r["dist_pct"] <= 6 and r["sma200"] == pytest.approx(x.sma200[near], abs=0.01)
    assert sma200.watch(panel, day, max_dist=0.001, always=("X",)) == []
    assert sma200.watch(panel, day) == [] or panel.composite(day)["X"] >= 0.5
    # history: touches with 60 sessions of follow-through only, counted once per 20 sessions
    assert sma200.history(x, touch + 59)["tests"] == 0
    h = sma200.history(x, len(x.c) - 1)
    assert h["tests"] == len([k for k in sma200.touches(x, len(x.c) - 61)])
    if h["tests"]:
        assert 0 <= h["held_rate"] <= 1 and h["last"]["date"] == x.dates[touch].isoformat()
    # the report lists it
    payload = {"asof": day.isoformat(), "regime": {"state": "ON", "size_mult": 1,
                                                   "reasons": ["x"]},
               "candidates": [], "sma200_watch": rows, "records": {}}
    assert "Near the 200-day average" in render(payload) and "X " in render(payload)
