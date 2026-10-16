"""Walk-forward grading of scanner ideas on stored history (SCANNER_REQUIREMENTS TS-11, TS-12).

An idea from bar i is traded on the following sessions only:
  * stop entry: fills on session i+1 if it trades through the trigger (at the open if the
    open is already past it); otherwise the idea expires unfilled. An open more than
    ``CHASE_R`` x R past the trigger cancels it (no chasing gaps, STRATEGY_SPEC §2 / EX-3);
  * open entry: fills at the open of session i+1;
  * before T1 the stop exits the whole position (a gap through the stop fills at the open);
    at T1 half is sold and the stop moves to break-even; the rest exits at T2, break-even or
    the close of the last session (day: 1 session, swing: ``max_hold`` sessions);
  * a bar that touches the stop and a target counts the stop first (STRATEGY_SPEC §8).
A session with minute bars is walked minute by minute, which removes most of that ambiguity.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from trader.scanner.techniques import MIN_HISTORY, Idea, Series, frames, ideas_at

OHLC = tuple[float, float, float, float]
CHASE_R = 0.5      # a session opening more than 0.5R past the trigger cancels the idea
MinuteLookup = Callable[[str, date], Sequence[OHLC] | None]


@dataclass(frozen=True)
class Outcome:
    filled: bool
    r: float = 0.0                 # realised R multiple of the whole position
    plus1r_first: bool = False     # +1R reached before -1R (AL-7 style hit)
    t1_hit: bool = False
    exit: str = "unfilled"         # stop | gap_stop | t2 | breakeven | time | unfilled
    sessions: int = 0
    minute_resolved: bool = False


def _flip(bar: OHLC, side: int) -> OHLC:
    o, h, l, c = bar  # noqa: E741
    return (o, h, l, c) if side > 0 else (-o, -l, -h, -c)


def grade(idea: Idea, sessions: Sequence[Sequence[OHLC]], minute_flags: Sequence[bool] = ()
          ) -> Outcome:
    """Grade one idea. ``sessions[k]`` holds the bars of session i+1+k: one daily bar, or
    that session's minute bars. Long and short share the code through mirrored prices."""
    side = 1 if idea.side == "long" else -1
    trig, stop = idea.trigger * side, idea.stop * side
    t1, t2 = idea.t1 * side, idea.t2 * side
    if not sessions:
        return Outcome(False)
    fill: float | None = None
    risk = 0.0
    banked = 0.0
    half_open = False             # True once T1 sold
    plus1r: bool | None = None
    cur_stop = stop
    used_minutes = bool(minute_flags and minute_flags[0])
    for k, sess in enumerate(sessions):
        used_minutes = used_minutes or bool(k < len(minute_flags) and minute_flags[k])
        for j, raw in enumerate(sess):
            o, h, l, c = _flip(raw, side)  # noqa: E741
            if fill is None:
                if k > 0:
                    return Outcome(False)
                if idea.entry == "open":
                    if j > 0:
                        return Outcome(False)
                    fill = o
                elif j == 0 and o > trig + CHASE_R * (trig - stop):
                    return Outcome(False, exit="gap_chase")   # never chase a gap (EX-3)
                elif h >= trig:
                    fill = max(o, trig)
                else:
                    continue
                risk = fill - stop
                if risk <= 0:
                    return Outcome(False)
            elif j == 0 and o <= cur_stop:               # gap through the stop
                r = (o - fill) / risk
                part = 0.5 if half_open else 1.0
                return Outcome(True, round(banked + part * r, 3), bool(plus1r), half_open,
                               "gap_stop" if not half_open else "breakeven", k + 1,
                               used_minutes)
            one_r = fill + risk
            if l <= cur_stop:                            # stop first on an ambiguous bar
                if plus1r is None:
                    plus1r = False
                part = 0.5 if half_open else 1.0
                r = (cur_stop - fill) / risk
                return Outcome(True, round(banked + part * r, 3), bool(plus1r), half_open,
                               "breakeven" if half_open else "stop", k + 1, used_minutes)
            if plus1r is None and h >= one_r:
                plus1r = True
            if not half_open and h >= t1:
                banked += 0.5 * (t1 - fill) / risk
                half_open, cur_stop = True, fill
            if half_open and h >= t2:
                banked += 0.5 * (t2 - fill) / risk
                return Outcome(True, round(banked, 3), bool(plus1r), True, "t2", k + 1,
                               used_minutes)
    if fill is None:
        return Outcome(False)
    last_c = _flip(sessions[-1][-1], side)[3]
    part = 0.5 if half_open else 1.0
    r = banked + part * (last_c - fill) / risk
    return Outcome(True, round(r, 3), bool(plus1r), half_open, "time", len(sessions),
                   used_minutes)


@dataclass
class Graded:
    idea: Idea
    outcome: Outcome


def walk_forward(series: dict[str, Series], spy: Series | None, *, start: date | None = None,
                 end: date | None = None, max_hold: int = 10,
                 minutes: MinuteLookup | None = None,
                 disabled: frozenset[str] = frozenset(),
                 edges: dict[str, float] | None = None) -> list[Graded]:
    """Every idea the scanner would have reported on each session, graded on what came next.
    The ideas for bar i come from the same code as the live scan (no look-ahead: TS-10)."""
    out: list[Graded] = []
    for sym, s in sorted(series.items()):
        if len(s.c) < MIN_HISTORY + 1:
            continue
        fr = frames(s, spy)
        for i in range(MIN_HISTORY - 1, len(s.c) - 1):
            d = s.dates[i]
            if (start and d < start) or (end and d > end):
                continue
            for idea in ideas_at(fr, i, disabled, edges=edges):
                hold = max_hold
                sess: list[Sequence[OHLC]] = []
                flags: list[bool] = []
                for k in range(i + 1, min(len(s.c), i + 1 + hold)):
                    mins = minutes(sym, s.dates[k]) if minutes else None
                    if mins:
                        sess.append(mins)
                        flags.append(True)
                    else:
                        sess.append([(float(s.o[k]), float(s.h[k]), float(s.l[k]),
                                      float(s.c[k]))])
                        flags.append(False)
                if len(sess) < hold:
                    continue                              # not enough future bars yet
                out.append(Graded(idea, grade(idea, sess, flags)))
    return out


def _stats(rows: list[Graded]) -> dict[str, Any]:
    filled = [g for g in rows if g.outcome.filled]
    rs = [g.outcome.r for g in filled]
    wins = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    return {
        "ideas": len(rows), "filled": len(filled),
        "fill_rate": round(len(filled) / len(rows), 3) if rows else None,
        "hit_1r": round(sum(g.outcome.plus1r_first for g in filled) / len(filled), 3)
        if filled else None,
        "t1_rate": round(sum(g.outcome.t1_hit for g in filled) / len(filled), 3)
        if filled else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "total_r": round(sum(rs), 2),
        "profit_factor": round(wins / losses, 2) if losses > 0 else (math.inf if wins else None),
        "avg_sessions": round(sum(g.outcome.sessions for g in filled) / len(filled), 1)
        if filled else None,
    }


SCORE_BUCKETS = ((0, 40), (40, 55), (55, 70), (70, 101))


def report(graded: list[Graded], min_samples: int = 30) -> dict[str, Any]:
    """TS-12: stats per technique and side, per style, per score bucket; weak techniques."""
    by_tech: dict[tuple[str, str, str], list[Graded]] = defaultdict(list)
    by_style: dict[str, list[Graded]] = defaultdict(list)
    by_bucket: dict[tuple[str, str], list[Graded]] = defaultdict(list)
    for g in graded:
        by_tech[(g.idea.style, g.idea.technique, g.idea.side)].append(g)
        by_style[g.idea.style].append(g)
        for lo, hi in SCORE_BUCKETS:
            if lo <= g.idea.score < hi:
                by_bucket[(g.idea.style, f"{lo}-{min(hi, 100)}")].append(g)
    techniques = []
    for (style, tech, side), rows in sorted(by_tech.items()):
        st = _stats(rows)
        st.update(style=style, technique=tech, side=side)
        st["weak"] = bool(st["filled"] >= min_samples and (st["avg_r"] or 0) < 0)
        techniques.append(st)
    return {
        "techniques": techniques,
        "styles": {k: _stats(v) for k, v in sorted(by_style.items())},
        "score_buckets": [dict(_stats(v), style=k[0], bucket=k[1])
                          for k, v in sorted(by_bucket.items())],
        "weak": sorted(f"{t['technique']}:{t['side']}" for t in techniques if t["weak"]),
        "minute_resolved": sum(g.outcome.minute_resolved for g in graded),
        "min_samples": min_samples,
    }


SHRINK = 30        # pseudo-trades of zero edge added to every technique's record


def edges_from(graded: list[Graded], min_filled: int = 5) -> dict[str, float]:
    """Measured edge per "TECHNIQUE:side" for the score (TS-8): average R shrunk toward zero
    by sample size, avg_r x n / (n + 30), so a few lucky trades cannot dominate."""
    acc: dict[str, list[float]] = defaultdict(list)
    for g in graded:
        if g.outcome.filled:
            acc[f"{g.idea.technique}:{g.idea.side}"].append(g.outcome.r)
    return {k: round(sum(v) / len(v) * len(v) / (len(v) + SHRINK), 3)
            for k, v in acc.items() if len(v) >= min_filled}


def load_edges(path: str) -> tuple[dict[str, float] | None, str | None]:
    """The edges written by ``trader scan-eval`` and a label for the report, or (None, None)."""
    import json
    from pathlib import Path

    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        edges = {str(k): float(v) for k, v in data["edges"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, None
    return edges, f"{p.name} ({str(data.get('generated', '?'))[:10]})"
