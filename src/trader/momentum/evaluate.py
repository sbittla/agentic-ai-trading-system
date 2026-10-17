"""Walk-forward grading of momentum setups, the alert stream and the ranking model
(MO-11..MO-14). Everything for session D uses bars up to D (MO-19); trades follow the same
state machine as the live tracker (exits.py)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import numpy as np

from trader import metrics
from trader.momentum import rotation
from trader.momentum.exits import POLICIES, Event, ExitParams, simulate
from trader.momentum.factors import FACTORS, MomFrame, Panel, crowded, regime
from trader.momentum.setups import SETUPS, setups_at
from trader.scanner.techniques import Series


@dataclass
class Trade:
    symbol: str
    setup: str
    policy: str
    signal: date
    entry_date: date
    exit_date: date | None
    entry: float
    risk: float
    r: float
    closed: bool
    sessions: int
    rs: int
    template: int
    leader: bool
    regime: str
    exit_reason: str
    events: list[Event] = field(default_factory=list, repr=False)

    def row(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "events"}
        for k in ("signal", "entry_date", "exit_date"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


def build_panel(series: Mapping[str, Series], spy: Series | None, *, min_price: float = 10.0,
                min_dollar: float = 50e6, rank_factors: Sequence[str] = ("r12_1", "clenow",
                                                                         "high52", "resid")
                ) -> Panel:
    frames = {k: MomFrame(v, spy) for k, v in series.items() if len(v.c) >= 60}
    return Panel(frames, min_price=min_price, min_dollar=min_dollar, rank_factors=rank_factors)


@dataclass(frozen=True)
class Signal:
    symbol: str
    index: int
    setup: str
    trigger: float
    stop: float
    entry: str
    rs: int
    template: int
    leader: bool
    regime: str


def signals(panel: Panel, spy: Series | None, *, min_rs: int = 80, min_template: int = 7,
            start: date | None = None, end: date | None = None,
            macro: Mapping[date, str] | None = None) -> list[Signal]:
    """Every setup on every session (leader filter recorded, not applied: MO-11 ablation)."""
    out: list[Signal] = []
    regimes: dict[date, str] = {}
    for sym, x in sorted(panel.frames.items()):
        for i in range(len(x.c) - 1):
            d = x.dates[i]
            if (start and d < start) or (end and d > end):
                continue
            if not x.liquid(i, panel.min_price, panel.min_dollar):
                continue
            rs = panel.rs(d).get(sym, 0)
            score, _ = x.template(i, rs)
            leader = rs >= min_rs and score >= min_template
            found = setups_at(x, i, leader=True)
            if not found:
                continue
            if d not in regimes:
                regimes[d] = regime(spy, d, (macro or {}).get(d)).state
            for st, cand in found:
                if st.needs_leader is False:
                    lead = True
                else:
                    lead = leader
                out.append(Signal(sym, i, st.name, cand.trigger, cand.stop, cand.entry, rs,
                                  score, lead, regimes[d]))
    return out


class Grader:
    """Memoised simulation per (signal, policy, entry-day rule)."""

    def __init__(self, panel: Panel, base: ExitParams) -> None:
        self.panel = panel
        self.base = base
        self._memo: dict[tuple[str, int, str, str, str], Trade | None] = {}

    def trade(self, sg: Signal, policy: str, entry_day_stop: str = "close") -> Trade | None:
        key = (sg.symbol, sg.index, sg.setup, policy, entry_day_stop)
        if key in self._memo:
            return self._memo[key]
        x = self.panel.frames[sg.symbol]
        prm = replace(self.base, policy=policy, entry_day_stop=entry_day_stop)  # type: ignore[arg-type]
        p, events = simulate(x, sg.index, sg.trigger, sg.stop, sg.entry, prm)
        t = None
        if p is not None:
            r = p.realized_r
            if not p.closed:                              # marked to the last close
                c = float(x.c[-1])
                r += p.open_frac * (c - p.entry) / p.risk
            ex = p.exit_index if p.closed else len(x.c) - 1
            t = Trade(sg.symbol, sg.setup, policy, x.dates[sg.index], x.dates[p.entry_index],
                      x.dates[p.exit_index] if p.closed else None, round(p.entry, 2),
                      round(p.risk, 4), round(r, 4), p.closed, ex - p.entry_index, sg.rs,
                      sg.template, sg.leader, sg.regime, p.exit_reason or "open", events)
        self._memo[key] = t
        return t


def take(signals_: Sequence[Signal], grader: Grader, setup: str, policy: str, *,
         leader: bool = True, regime_filter: bool = True, entry_day_stop: str = "close"
         ) -> list[Trade]:
    """One trade per symbol at a time for a setup x policy (a re-arm while in a trade is
    ignored, as a trader would)."""
    busy: dict[str, int] = {}
    out = []
    for sg in signals_:
        if sg.setup != setup or (leader and not sg.leader):
            continue
        if regime_filter and sg.regime == "OFF":
            continue
        if sg.index + 1 <= busy.get(sg.symbol, -1):
            continue
        t = grader.trade(sg, policy, entry_day_stop)
        if t is None:
            continue
        x = grader.panel.frames[sg.symbol]
        busy[sg.symbol] = x.pos[t.exit_date] if t.exit_date else len(x.c)
        out.append(t)
    return out


def stats(trades: Sequence[Trade]) -> dict[str, Any]:
    rs = [t.r for t in trades]
    if not rs:
        return {"trades": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    streak = longest = 0
    for t in sorted(trades, key=lambda t: t.entry_date):
        streak = streak + 1 if t.r <= 0 else 0
        longest = max(longest, streak)
    gl = -sum(losses)
    return {
        "trades": len(rs), "win_rate": round(len(wins) / len(rs), 3),
        "avg_r": round(float(np.mean(rs)), 3), "median_r": round(float(np.median(rs)), 3),
        "avg_win": round(float(np.mean(wins)), 3) if wins else None,
        "avg_loss": round(float(np.mean(losses)), 3) if losses else None,
        "profit_factor": round(sum(wins) / gl, 2) if gl > 0 else None,
        "total_r": round(sum(rs), 1), "best_r": round(max(rs), 2),
        "avg_sessions": round(float(np.mean([t.sessions for t in trades])), 1),
        "max_consecutive_losses": longest, "open": sum(1 for t in trades if not t.closed),
        "ci95": metrics.bootstrap_ci(rs),
    }


def halves(trades: Sequence[Trade], split: date) -> dict[str, Any]:
    return {"is": stats([t for t in trades if t.signal < split]),
            "oos": stats([t for t in trades if t.signal >= split]),
            "all": stats(trades)}


def gate(h: Mapping[str, Any], min_trades: int = 30, min_pf: float = 1.2) -> dict[str, Any]:
    """MO-14 alert gate on a setup's default policy."""
    a, i, o = h["all"], h["is"], h["oos"]
    checks = {
        f"trades_ge_{min_trades}": a.get("trades", 0) >= min_trades,
        "avg_r_gt_0_in_sample": (i.get("avg_r") or 0) > 0,
        "avg_r_gt_0_out_of_sample": (o.get("avg_r") or 0) > 0,
        f"profit_factor_ge_{min_pf}": (a.get("profit_factor") or 0) >= min_pf,
    }
    return {"checks": checks, "pass": all(checks.values())}


# --- MO-12 alert-stream portfolio --------------------------------------------------------

def _curve_stats(dates: Sequence[date], equity: Sequence[float], split: date | None = None
                 ) -> dict[str, Any]:
    rets = metrics.daily_returns(list(equity))
    out = metrics.risk_adjusted(rets, [])
    out.pop("sqn", None)
    out.pop("max_consecutive_losses", None)
    out["total_return_pct"] = round((equity[-1] / equity[0] - 1) * 100, 1) if equity else None
    out["start"], out["end"] = (dates[0].isoformat(), dates[-1].isoformat()) if dates else (
        None, None)
    if split is not None and dates:
        k = next((j for j, d in enumerate(dates) if d >= split), len(dates))
        if 20 < k < len(dates) - 20:
            out["is"] = _curve_stats(dates[:k + 1], equity[:k + 1])
            out["oos"] = _curve_stats(dates[k:], equity[k:])
    return out


def benchmarks(panel: Panel, spy: Series | None, dates: Sequence[date]) -> dict[str, list[float]]:
    """SPY buy-and-hold and the daily-rebalanced equal-weight universe over ``dates``."""
    out: dict[str, list[float]] = {}
    if spy is not None:
        idx = {d: j for j, d in enumerate(spy.dates)}
        base = None
        curve = []
        for d in dates:
            j = idx.get(d)
            if j is not None:
                base = base or float(spy.c[j])
                curve.append(float(spy.c[j]) / base)
            else:
                curve.append(curve[-1] if curve else 1.0)
        out["spy"] = curve
    ew = [1.0]
    for a, b in zip(dates, dates[1:], strict=False):
        rets = []
        for x in panel.frames.values():
            i, j = x.pos.get(a), x.pos.get(b)
            if i is not None and j is not None and x.liquid(i, panel.min_price, panel.min_dollar):
                rets.append(float(x.c[j] / x.c[i] - 1))
        ew.append(ew[-1] * (1 + (float(np.mean(rets)) if rets else 0.0)))
    out["equal_weight"] = ew
    return out


def portfolio(trades: Sequence[Trade], panel: Panel, dates: Sequence[date], *,
              risk_pct: float = 1.0, max_positions: int = 8, max_heat_pct: float = 6.0,
              max_position_pct: float = 25.0, cost_bps: float = 10.0,
              max_correlated: int = 2) -> dict[str, Any]:
    """MO-12: the alert stream traded with slots, risk sizing, a heat cap, the correlation
    cap and no leverage. Cash accounting: equity = cash + shares x close."""
    by_entry: dict[date, list[Trade]] = defaultdict(list)
    for t in trades:
        by_entry[t.entry_date].append(t)
    cash, equity = 1.0, []
    open_: list[dict[str, Any]] = []
    taken = skipped = 0
    rs_taken: list[float] = []
    exposure: list[float] = []
    for d in dates:
        # sales and stop-to-break-even recorded by the trade's events on this date
        for pos in list(open_):
            x = panel.frames[pos["t"].symbol]
            for ev in pos["by_date"].get(d, []):
                if ev.kind == "RAISE_STOP":
                    if ev.price >= pos["t"].entry:
                        pos["free"] = True
                    continue
                if ev.frac <= 0:                          # a decision, sold the next day
                    continue
                units = pos["units0"] * ev.frac
                units = min(units, pos["units"])
                cash += units * ev.price * (1 - cost_bps / 1e4)
                pos["units"] -= units
            if pos["units"] <= 1e-12 or (pos["t"].exit_date and d >= pos["t"].exit_date):
                if pos["units"] > 1e-12:                 # residue: close at the close
                    i = x.pos.get(d)
                    if i is not None:
                        cash += pos["units"] * float(x.c[i]) * (1 - cost_bps / 1e4)
                rs_taken.append(pos["t"].r)
                open_.remove(pos)
        eq_now = cash + sum(p["units"] * _close(panel, p["t"].symbol, d, p) for p in open_)
        for t in sorted(by_entry.get(d, []), key=lambda t: (-t.rs, t.symbol)):
            heat = sum(risk_pct for p in open_ if not p["free"])
            held = [p["t"].symbol for p in open_]
            if len(open_) >= max_positions or heat + risk_pct > max_heat_pct + 1e-9 or \
                    t.symbol in held or crowded(panel.frames, t.symbol, held, d,
                                                max_correlated) is not None:
                skipped += 1
                continue
            units = eq_now * risk_pct / 100 / t.risk
            units = min(units, eq_now * max_position_pct / 100 / t.entry,
                        max(cash, 0.0) / (t.entry * (1 + cost_bps / 1e4)))
            if units <= 0:
                skipped += 1
                continue
            cash -= units * t.entry * (1 + cost_bps / 1e4)
            evs: dict[date, list[Event]] = defaultdict(list)
            x = panel.frames[t.symbol]
            for ev in t.events:
                if ev.kind != "ENTER":
                    evs[x.dates[ev.index]].append(ev)
            open_.append({"t": t, "units0": units, "units": units, "by_date": evs,
                          "free": False, "last": t.entry})
            taken += 1
            # entry-day sales (a stop on the fill day)
            for ev in evs.pop(d, []):
                if ev.frac > 0:
                    u = min(units * ev.frac, open_[-1]["units"])
                    cash += u * ev.price * (1 - cost_bps / 1e4)
                    open_[-1]["units"] -= u
        value = sum(p["units"] * _close(panel, p["t"].symbol, d, p) for p in open_)
        equity.append(cash + value)
        exposure.append(value / (cash + value) if cash + value > 0 else 0.0)
    return {"equity": equity, "taken": taken, "skipped_for_slots_or_heat": skipped,
            "avg_exposure": round(float(np.mean(exposure)), 3) if exposure else None,
            "trade_r": rs_taken}


def _close(panel: Panel, sym: str, d: date, pos: dict[str, Any]) -> float:
    x = panel.frames[sym]
    i = x.pos.get(d)
    if i is not None:
        pos["last"] = float(x.c[i])
    return float(pos["last"])


# --- MO-13 / MO-25 rotation model ----------------------------------------------------------

def _half_cagr(st: Mapping[str, Any], half: str) -> float:
    v = (st.get(half) or {}).get("cagr_pct")
    return float(v) if v is not None else -1e9


def rotation_grid(panel: Panel, spy: Series | None, cal: Sequence[date], split: date,
                  bench: Mapping[str, Mapping[str, Any]], *, rank_factors: Sequence[str],
                  top_n: int, hold_rank: int, weeks: int, cost_bps: float,
                  max_weight: float = 0.2) -> dict[str, Any]:
    """MO-13: every single factor, the configured composite and the composite of the three
    best single factors in-sample, each with and without the regime filter. The model used
    live is the one with the best in-sample CAGR: chosen without the out-of-sample half."""
    def one(fs: Sequence[str], rf: bool) -> dict[str, Any]:
        r = rotation.run(panel, spy, cal, factors=fs, top_n=top_n, hold_rank=hold_rank,
                         weeks=weeks, cost_bps=cost_bps, regime_filter=rf,
                         max_weight=max_weight)
        st = _curve_stats(cal, r.equity, split)
        st.update(turnover_per_year=r.turnover_per_year, invested_share=r.invested_share,
                  factors=list(fs), regime_filter=rf)
        return st
    runs: dict[str, Any] = {}
    for f in FACTORS:
        for rf in (True, False):
            runs[f"{f}{'' if rf else ' (no regime)'}"] = one((f,), rf)
    top3 = sorted(FACTORS, key=lambda f: -_half_cagr(runs[f], "is"))[:3]
    for label, fs in (("composite", tuple(rank_factors)), ("top3_in_sample", tuple(top3))):
        for rf in (True, False):
            runs[f"{label}{'' if rf else ' (no regime)'}"] = one(fs, rf)
    best = max(runs, key=lambda k: _half_cagr(runs[k], "is"))
    sel = runs[best]
    oos_rank = 1 + sum(1 for v in runs.values() if _half_cagr(v, "oos") > _half_cagr(sel, "oos"))
    checks = {}
    for half in ("is", "oos"):
        for b in ("spy", "equal_weight"):
            checks[f"cagr_beats_{b}_{half}"] = _half_cagr(sel, half) > _half_cagr(bench[b], half)
    return {"runs": runs, "selected": {"name": best, "factors": sel["factors"],
                                       "regime_filter": sel["regime_filter"],
                                       "oos_rank": f"{oos_rank} of {len(runs)}"},
            "gate": {"checks": checks, "pass": all(checks.values())}}


# --- the whole evaluation ------------------------------------------------------------------

def evaluate(series: Mapping[str, Series], spy: Series | None, *, params: ExitParams,
             setup_policy: Mapping[str, str], min_rs: int = 80, min_template: int = 7,
             min_price: float = 10.0, min_dollar: float = 50e6,
             rank_factors: Sequence[str] = ("r12_1", "clenow", "high52", "resid"),
             start: date | None = None, end: date | None = None, min_trades: int = 30,
             min_pf: float = 1.2, risk_pct: float = 1.0, max_positions: int = 8,
             max_heat_pct: float = 6.0, max_position_pct: float = 25.0, top_n: int = 10,
             hold_rank: int = 20, rebalance_weeks: int = 2, policies: Sequence[str] = POLICIES,
             max_correlated: int = 2, max_weight: float = 0.2,
             macro: Mapping[date, str] | None = None, watch_only: Sequence[str] = ()
             ) -> dict[str, Any]:
    panel = build_panel(series, spy, min_price=min_price, min_dollar=min_dollar,
                        rank_factors=rank_factors)
    sigs = signals(panel, spy, min_rs=min_rs, min_template=min_template, start=start, end=end,
                   macro=macro)
    if spy is None:
        return {"error": "no SPY bars"}
    cal = [d for d in spy.dates if (not start or d >= start) and (not end or d <= end)]
    first = next((d for d in cal if len(panel.eligible(d)) >= 10 and panel.composite(d)), None)
    if first is None:
        return {"error": "not enough history (need 260 sessions on 10+ symbols)"}
    cal = [d for d in cal if d >= first]
    split = cal[len(cal) // 2]
    sigs = [s for s in sigs if panel.frames[s.symbol].dates[s.index] >= first]
    grader = Grader(panel, params)
    setups: dict[str, Any] = {}
    alerted: list[Trade] = []
    gate_pass: list[str] = []
    for st in SETUPS:
        pol = setup_policy.get(st.name, st.policy)
        res: dict[str, Any] = {"default_policy": pol, "policies": {}}
        for p in policies:
            res["policies"][p] = halves(take(sigs, grader, st.name, p), split)
        dflt = take(sigs, grader, st.name, pol)
        res["gate"] = gate(res["policies"][pol], min_trades, min_pf)
        res["ablation"] = {
            "no_leader_filter": halves(take(sigs, grader, st.name, pol, leader=False), split)
            if st.needs_leader else None,
            "no_regime_filter": halves(take(sigs, grader, st.name, pol, regime_filter=False),
                                       split),
            "pessimistic_entry_day": halves(take(sigs, grader, st.name, pol,
                                                 entry_day_stop="low"), split),
        }
        setups[st.name] = res
        if res["gate"]["pass"]:
            gate_pass.append(st.name)
            if st.name not in watch_only:     # MO-26: the stream is what gets alerted
                alerted += dflt
    bench = benchmarks(panel, spy, cal)
    bench_stats = {k: _curve_stats(cal, v, split) for k, v in bench.items()}
    stream: dict[str, Any] = {}
    every = [t for st in SETUPS for t in take(sigs, grader, st.name,
                                                setup_policy.get(st.name, st.policy))]
    for label, trs in (("gated_setups", alerted), ("all_setups", every)):
        pf = portfolio(trs, panel, cal, risk_pct=risk_pct, max_positions=max_positions,
                       max_heat_pct=max_heat_pct, max_position_pct=max_position_pct,
                       cost_bps=params.cost_bps, max_correlated=max_correlated)
        st_ = _curve_stats(cal, pf["equity"], split)
        st_.update(taken=pf["taken"], skipped=pf["skipped_for_slots_or_heat"],
                   avg_exposure=pf["avg_exposure"],
                   gate=metrics.statistical_gate(pf["trade_r"], risk_pct, 10.0)
                   if pf["trade_r"] else None)
        stream[label] = st_
    rot = rotation_grid(panel, spy, cal, split, bench_stats, rank_factors=rank_factors,
                        top_n=top_n, hold_rank=hold_rank, weeks=rebalance_weeks,
                        cost_bps=params.cost_bps, max_weight=max_weight)
    return {
        "period": {"start": cal[0].isoformat(), "end": cal[-1].isoformat(),
                   "split": split.isoformat(), "sessions": len(cal),
                   "symbols": len(panel.frames)},
        "signals": len(sigs), "setups": setups, "gate_pass": gate_pass,
        "watch_only": [n for n in watch_only if n in setups],
        "alert_stream": stream, "rotation": rot, "benchmarks": bench_stats,
        "params": {k: v for k, v in params.__dict__.items()},
        "survivorship_note": "The universe is today's large caps (survivorship bias): compare "
                             "with equal_weight, not only SPY.",
    }
