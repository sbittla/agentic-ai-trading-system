"""The momentum entry and exit state machine (MO-8, MO-9). One session at a time; the same
functions run the live tracker and the walk-forward backtest, so they cannot drift apart.

Prices are real (long only). R = entry - initial stop. A position's ``realized_r`` is the sum
over its sales of (fraction sold x (price - entry) / R), after costs.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from trader.momentum.factors import MomFrame

POLICIES = ("ema10", "ema20", "sma50", "chandelier", "donchian20")
CHASE_R = 0.5
MIN_R_ATR = 0.25     # MO-8: a fill this close to the stop is skipped (R would be noise)


@dataclass(frozen=True)
class ExitParams:
    policy: str = "ema20"
    partial_frac: float = 1 / 3
    partial_r: float = 2.0
    partial_days: int = 5
    climax_atr: float = 7.0
    climax_frac: float = 0.5
    time_stop_days: int = 10
    max_hold_days: int = 250
    max_loss_pct: float = 0.08
    cost_bps: float = 10.0
    entry_day_stop: Literal["close", "low"] = "close"   # see enter()


@dataclass
class Event:
    kind: str   # ENTER SKIP EXPIRE STOP PARTIAL RAISE_STOP TRAIL CLIMAX TIME MAX_HOLD
    index: int
    price: float
    frac: float = 0.0  # fraction of the original position sold
    r: float = 0.0     # R of that sale (after costs)
    note: str = ""
    at_open_next: bool = False   # decided on the close; sold at the next session's open
    executed: bool = False       # the sale of an earlier at_open_next decision

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    entry: float
    entry_index: int
    stop: float
    risk: float
    policy: str
    open_frac: float = 1.0
    realized_r: float = 0.0
    partial_done: bool = False
    climax_done: bool = False
    high_since: float = 0.0
    max_r: float = 0.0
    last_index: int = -1
    pending: list[dict[str, Any]] = field(default_factory=list)   # sales at the next open
    closed: bool = False
    exit_reason: str = ""
    exit_index: int = -1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _cost_r(p: Position, px: float, frac: float, bps: float) -> float:
    return frac * (p.entry + px) * bps / 1e4 / p.risk


def _sell(p: Position, i: int, px: float, frac: float, kind: str, prm: ExitParams,
          note: str = "") -> Event:
    frac = min(frac, p.open_frac)
    r = frac * (px - p.entry) / p.risk - _cost_r(p, px, frac, prm.cost_bps)
    p.realized_r += r
    p.open_frac = round(p.open_frac - frac, 9)
    if p.open_frac <= 1e-9:
        p.open_frac = 0.0
        p.closed, p.exit_reason, p.exit_index = True, kind, i
    return Event(kind, i, round(px, 2), round(frac, 4), round(r, 4), note)


def enter(x: MomFrame, i: int, trigger: float, stop: float, entry: str, prm: ExitParams
          ) -> tuple[Position | None, list[Event]]:
    """MO-8: fill on session i (the session after the setup). A buy-stop fills at
    max(open, trigger) if the high reaches it; an open more than 0.5R above the trigger is
    skipped (no chasing). The stop is raised to the max-loss cap.

    On the fill day the order of the low and the trigger is unknown from a daily bar. With
    ``entry_day_stop="close"`` the stop counts only if the close is at or under it (or the
    fill was at the open, so the low came after it); ``"low"`` counts any low at the stop
    (pessimistic). The evaluation reports both (MO-11)."""
    o, h, lo, c = float(x.o[i]), float(x.h[i]), float(x.l[i]), float(x.c[i])
    if entry == "open":
        fill = o
    else:
        if o > trigger + CHASE_R * (trigger - stop):
            return None, [Event("SKIP", i, round(o, 2), note=f"opened {o:.2f}, more than 0.5R "
                                f"above the trigger {trigger:.2f}: not chased")]
        if h < trigger:
            return None, [Event("EXPIRE", i, round(h, 2), note="trigger not reached")]
        fill = max(o, trigger)
    stop = max(stop, fill * (1 - prm.max_loss_pct))
    a = float(x.atr[i - 1]) if i > 0 else math.nan
    if fill - stop < (MIN_R_ATR * a if math.isfinite(a) else 1e-9):
        return None, [Event("SKIP", i, round(fill, 2), note="opened at the stop: R under "
                            f"{MIN_R_ATR} ATR")]
    p = Position(entry=fill, entry_index=i, stop=stop, risk=fill - stop, policy=prm.policy,
                 high_since=h, last_index=i)
    evs = [Event("ENTER", i, round(fill, 2), note=f"stop {stop:.2f}, R {fill - stop:.2f}")]
    at_open = entry == "open" or o >= trigger
    stopped = lo <= stop if (at_open or prm.entry_day_stop == "low") else c <= stop
    if stopped:
        evs.append(_sell(p, i, stop, 1.0, "STOP", prm, "stopped on the entry day"))
        return p, evs
    _close_rules(x, i, p, prm, evs)
    return p, evs


def _trail_level(x: MomFrame, i: int, p: Position) -> float | None:
    """Intraday trailing stop for the next session, from bars up to i."""
    if p.policy == "chandelier":
        a = float(x.atr22[i])
        return None if math.isnan(a) else p.high_since - 3 * a
    if p.policy == "donchian20":
        lvl = float(x.l[max(0, i - 19):i + 1].min())
        return lvl
    return None


def _ma(x: MomFrame, policy: str, i: int) -> float | None:
    arr = {"ema10": x.ema10, "ema20": x.ema20, "sma50": x.sma50}.get(policy)
    if arr is None:
        return None
    v = float(arr[i])
    return None if math.isnan(v) else v


def _raise(p: Position, i: int, new: float, why: str, events: list[Event]) -> None:
    if new > p.stop + 1e-9:
        moved = new - p.stop
        p.stop = new
        if moved >= 0.25 * p.risk or why == "break-even":
            events.append(Event("RAISE_STOP", i, round(new, 2), note=why))


def _close_rules(x: MomFrame, i: int, p: Position, prm: ExitParams, events: list[Event]
                 ) -> None:
    """Decisions taken on bar i's close (MO-9 steps 2b-6)."""
    if p.closed:
        return
    c = float(x.c[i])
    held = i - p.entry_index
    p.max_r = max(p.max_r, (p.high_since - p.entry) / p.risk)
    if not p.partial_done and held >= prm.partial_days and c > p.entry and not any(
            q["kind"] == "PARTIAL" for q in p.pending):
        note = f"{held} sessions in profit: sell {prm.partial_frac:.0%}, stop to break-even"
        p.pending.append({"kind": "PARTIAL", "frac": prm.partial_frac, "note": note})
        events.append(Event("PARTIAL", i, round(c, 2), note=note, at_open_next=True))
        return
    s50, a = float(x.sma50[i]), float(x.atr[i])
    if not p.climax_done and not math.isnan(s50) and c >= s50 + prm.climax_atr * a:
        p.climax_done = True
        p.policy = "ema10"
        p.pending.append({"kind": "CLIMAX", "frac": p.open_frac * prm.climax_frac,
                          "note": f"close {c:.2f} is {(c - s50) / a:.1f} ATR above SMA50: "
                                  "sell into strength, trail on EMA10"})
        events.append(Event("CLIMAX", i, round(c, 2), note=p.pending[-1]["note"],
                            at_open_next=True))
    ma = _ma(x, p.policy, i)
    if ma is not None and p.partial_done and c < ma:
        p.pending.append({"kind": "TRAIL", "frac": 1.0,
                          "note": f"close {c:.2f} under {p.policy.upper()} {ma:.2f}"})
        events.append(Event("TRAIL", i, round(c, 2), note=p.pending[-1]["note"],
                            at_open_next=True))
        return
    lvl = _trail_level(x, i, p)
    if lvl is not None:
        _raise(p, i, lvl, f"{p.policy} trail", events)
    if held >= prm.time_stop_days and p.max_r < 1.0 and not p.partial_done:
        p.pending.append({"kind": "TIME", "frac": 1.0,
                          "note": f"{held} sessions without +1R"})
        events.append(Event("TIME", i, round(c, 2), note=p.pending[-1]["note"],
                            at_open_next=True))
        return
    if held >= prm.max_hold_days:
        p.pending.append({"kind": "MAX_HOLD", "frac": 1.0, "note": f"{held} sessions"})
        events.append(Event("MAX_HOLD", i, round(c, 2), at_open_next=True))


def step(x: MomFrame, i: int, p: Position, prm: ExitParams, intrabar: bool = True
         ) -> list[Event]:
    """MO-9: apply session i (after the entry session). ``intrabar=False`` skips the stop and
    target checks (the live monitor already saw them during the session)."""
    events: list[Event] = []
    if p.closed or i <= p.last_index:
        return events
    p.last_index = i
    o, h, lo = float(x.o[i]), float(x.h[i]), float(x.l[i])
    if p.pending:                                       # decided at the prior close
        pend, p.pending = p.pending, []
        for q in pend:
            if p.closed:
                break
            frac = p.open_frac if q["frac"] >= 1.0 else q["frac"]
            ev = _sell(p, i, o, frac, str(q["kind"]), prm, f"{q['note']}: sold at the open")
            ev.executed = True
            events.append(ev)
            if q["kind"] == "PARTIAL" and not p.closed:
                p.partial_done = True
                _raise(p, i, p.entry, "break-even", events)
        if p.closed:
            return events
    if intrabar:
        if o <= p.stop:
            events.append(_sell(p, i, o, p.open_frac, "STOP", prm, "gapped through the stop"))
            return events
        if lo <= p.stop:                                # stop first on an ambiguous bar
            kind = "STOP"
            events.append(_sell(p, i, p.stop, p.open_frac, kind, prm,
                                "stop hit" if p.stop < p.entry else "trailing stop hit"))
            return events
        target = p.entry + prm.partial_r * p.risk
        if not p.partial_done and h >= target:
            events.append(_sell(p, i, max(o, target), prm.partial_frac, "PARTIAL", prm,
                                f"+{prm.partial_r:g}R target"))
            p.partial_done = True
            _raise(p, i, p.entry, "break-even", events)
    p.high_since = max(p.high_since, h)
    _close_rules(x, i, p, prm, events)
    return events


def simulate(x: MomFrame, i: int, trigger: float, stop: float, entry: str, prm: ExitParams
             ) -> tuple[Position | None, list[Event]]:
    """Backtest one setup armed on bar i: enter on i+1, step to the exit or the last bar."""
    if i + 1 >= len(x.c):
        return None, []
    p, events = enter(x, i + 1, trigger, stop, entry, prm)
    if p is None:
        return None, events
    k = i + 2
    while not p.closed and k < len(x.c):
        events += step(x, k, p, prm)
        k += 1
    return p, events
