"""Catalyst strength 0-100 (NC-7..NC-10). Code only; every factor and its points are kept so
the score explains itself."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trader.catalyst.stories import Story, source_weight

MATERIALITY = {"GUIDANCE": 20, "EARNINGS": 18, "M_AND_A": 18, "REGULATORY_LEGAL": 15,
               "MANAGEMENT": 12, "ANALYST": 10, "CAPITAL": 10, "PRODUCT": 10, "INSIDER": 8,
               "MACRO_SECTOR": 8, "OTHER": 4, "SOCIAL_ONLY": 3}
HARD_TYPES = frozenset({"EARNINGS", "GUIDANCE", "M_AND_A", "MANAGEMENT", "ANALYST"})
LABELS = ((80, "MAJOR"), (60, "STRONG"), (40, "MODERATE"), (0, "WEAK"))


@dataclass
class Reaction:
    """The market's first verdict on the session the story belongs to (day 0)."""

    ret: float                 # close-to-close return of day 0
    abnormal: float            # ret - beta x SPY return
    sigma: float               # daily volatility before the story
    rvol: float | None         # day-0 volume / 50-day average
    atr_move: float | None     # |close - prior close| / ATR14
    close_loc: float | None    # (close - low) / (high - low)


@dataclass
class Strength:
    score: float
    label: str
    factors: dict[str, float] = field(default_factory=dict)
    priced_in: bool = False
    hard_event: bool = False


def label(score: float) -> str:
    return next(name for floor, name in LABELS if score >= floor)


def surprise_points(eps_surprise_pct: float | None, pt_change_pct: float | None,
                    rating_step: int | None, guidance: bool) -> float:
    """NC-8: the size of the surprise, 0-20, from whatever the story carries."""
    pts = 0.0
    if eps_surprise_pct is not None:
        pts = max(pts, 20 * min(1.0, abs(eps_surprise_pct) / 10))
    if pt_change_pct is not None:
        pts = max(pts, 20 * min(1.0, abs(pt_change_pct) / 20))
    if rating_step:
        pts = max(pts, 6.0 * min(2, abs(rating_step)))
    if guidance:
        pts = max(pts, 12.0)
    return round(pts, 1)


def score(story: Story, ctype: str, sentiment: float, *, weights: Mapping[str, float],
          default_w: float, reaction: Reaction | None = None, direction: str = "mixed",
          surprise: float = 0.0, social_points: float = 0.0, implied_1d: float | None = None,
          ) -> Strength:
    """NC-7, NC-8, NC-10."""
    f: dict[str, float] = {}
    f["materiality"] = float(MATERIALITY.get(ctype, 4))
    f["surprise"] = round(min(20.0, surprise), 1)
    f["novelty"] = round(15 * story.novelty, 1)
    pubs = story.publishers
    best_w = max((source_weight(p, weights, default_w) for p in pubs), default=0.0)
    f["sources"] = round(10 * min(1.0, 0.6 * best_w + 0.4 * min(1.0, len(pubs) / 5)), 1)
    f["sentiment"] = round(10 * min(1.0, abs(sentiment) / 0.6), 1)
    confirm = 0.0
    if reaction is not None and reaction.sigma > 0 and direction != "mixed":
        z = reaction.abnormal / reaction.sigma
        agrees = (z > 0) == (direction == "positive")
        if agrees:
            confirm = 10 * min(1.0, abs(z) / 3)
            if reaction.rvol is not None:
                confirm += 5 * min(1.0, max(0.0, reaction.rvol - 1) / 2)
    f["confirmation"] = round(confirm, 1)
    f["social"] = round(min(5.0, max(0.0, social_points)), 1)
    day1 = [i for i in story.items
            if timedelta(hours=16) <= i.published_at - story.first_seen <= timedelta(hours=48)]
    f["persistence"] = 5.0 if len(day1) >= 2 else 3.0 if day1 else 0.0
    total = round(min(100.0, sum(f.values())), 1)
    priced = False
    if reaction is not None:
        big_vs_iv = implied_1d is not None and implied_1d > 0 and \
            abs(reaction.ret) > 1.5 * implied_1d
        big_vs_atr = reaction.atr_move is not None and reaction.atr_move > 3
        priced = big_vs_iv or big_vs_atr
    hard = ctype in HARD_TYPES or "8-K" in story.kinds
    return Strength(total, label(total), f, priced, hard)


def decayed(score_value: float, last_seen: datetime, now: datetime, half_life_h: float
            ) -> float:
    """NC-7: a story fades with no new coverage; half-life in hours."""
    age = max(0.0, (now - last_seen).total_seconds() / 3600)
    return round(score_value * math.pow(0.5, age / half_life_h), 1) if half_life_h > 0 \
        else score_value
