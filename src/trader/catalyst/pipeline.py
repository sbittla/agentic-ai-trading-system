"""The catalyst pipeline as pure functions of its inputs (A17; NC-1..NC-26).

Inputs come from the journal and the bar store (``agent.py``); everything here is
deterministic so a past day can be rebuilt exactly (no look-ahead: callers pass only data
published before the as-of time).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np

from trader.catalyst import context as cx
from trader.catalyst.classify import classify_rules, direction
from trader.catalyst.outlook import (
    EventObs,
    HorizonView,
    Pool,
    beta_at,
    blended_sigma,
    day0_index,
    ewma_sigma,
    horizon_view,
    observe,
    reaction,
    scenarios,
)
from trader.catalyst.position import View, plan, stance, vehicle
from trader.catalyst.social import SocialDay, agreement, crowded
from trader.catalyst.stories import Item, Story, cluster, score_novelty
from trader.catalyst.strength import Reaction, Strength, decayed, score, surprise_points
from trader.config import CatalystCfg
from trader.scanner import ta
from trader.scanner.techniques import Series


@dataclass
class Typed:
    """A story with its type, direction, strength and day-0 reaction."""

    story: Story
    ctype: str
    type_source: str
    direction: str
    sentiment: float
    strength: Strength
    i0: int | None = None
    reaction: Reaction | None = None
    beta: float = 1.0
    social: SocialDay | None = None
    agreement: str = "QUIET"
    crowding: str | None = None


@dataclass
class Inputs:
    """Everything known at the as-of time, per ticker."""

    series: Mapping[str, Series]
    spy: Series | None = None
    sector: Mapping[str, Series] = field(default_factory=dict)          # ticker -> ETF
    iv: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)    # options_iv_daily
    actions: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    fundamentals: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    social: Mapping[str, Sequence[SocialDay]] = field(default_factory=dict)
    earnings_next: Mapping[str, int | None] = field(default_factory=dict)   # sessions away
    macro: Sequence[tuple[int, str]] = ()          # (sessions away, event name)
    llm_types: Mapping[str, tuple[str, str]] = field(default_factory=dict)  # story -> type


@dataclass
class Outlook:
    outlook_id: str
    ticker: str
    as_of: date
    typed: Typed
    strength_now: float
    views: list[HorizonView]
    confluence: list[dict[str, Any]]
    view: View
    scenarios: list[str]
    calibrated: bool
    last: float
    day0: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        t = self.typed
        return {
            "outlook_id": self.outlook_id, "ticker": self.ticker,
            "as_of": self.as_of.isoformat(), "story_id": t.story.story_id,
            "headline": t.story.headline, "first_seen": t.story.first_seen.isoformat(),
            "items": len(t.story.items), "sources": len(t.story.publishers),
            "ctype": t.ctype, "type_source": t.type_source, "direction": t.direction,
            "sentiment": t.sentiment, "novelty": t.story.novelty,
            "strength": t.strength.score, "strength_now": self.strength_now,
            "label": t.strength.label, "factors": t.strength.factors,
            "priced_in": t.strength.priced_in,
            "reaction": None if t.reaction is None else {
                "ret_pct": round(t.reaction.ret * 100, 2),
                "abnormal_pct": round(t.reaction.abnormal * 100, 2),
                "rvol": None if t.reaction.rvol is None else round(t.reaction.rvol, 2)},
            "social": None if t.social is None else t.social.as_dict(),
            "agreement": t.agreement, "crowding": t.crowding,
            "horizons": [v.as_dict() for v in self.views],
            "confluence": self.confluence, "tally": cx.tally(self.confluence),
            "view": self.view.as_dict(), "scenarios": self.scenarios,
            "calibrated": self.calibrated, "last": self.last, "day0": self.day0}


def build_stories(items: Sequence[Item], cfg: CatalystCfg) -> list[Story]:
    stories = cluster(items, join=cfg.story_join, join_hash=cfg.story_join_hash,
                      window_h=cfg.story_window_h)
    score_novelty(stories, cfg.novelty_days)
    return stories


def _surprise(ctype: str, story: Story, inp: Inputs, last: float) -> float:
    t = story.ticker
    eps = None
    if ctype == "EARNINGS":
        hist = (inp.fundamentals.get(t) or {}).get("earnings") or []
        if hist and hist[0].get("surprisePercent") is not None:
            eps = float(hist[0]["surprisePercent"])
    pt = step = None
    if ctype == "ANALYST":
        acts = [a for a in inp.actions.get(t, [])
                if a.get("ts") is not None and abs((a["ts"] - story.first_seen)
                                                   .total_seconds()) <= 3 * 86400]
        for a in acts:
            if a.get("pt_new") and a.get("pt_old") and float(a["pt_old"]) > 0:
                pt = (float(a["pt_new"]) / float(a["pt_old"]) - 1) * 100
            elif a.get("pt_new") and last > 0:
                pt = (float(a["pt_new"]) / last - 1) * 100
            act = str(a.get("action", "")).lower()
            step = 1 if act in ("up", "upgrade") else -1 if act in ("down", "downgrade") \
                else step
    return surprise_points(eps, pt, step, ctype == "GUIDANCE")


def type_story(story: Story, cfg: CatalystCfg, inp: Inputs) -> Typed:
    """NC-5..NC-10 for one story."""
    rule = classify_rules(story)
    dirn, sent = direction(story, cfg.source_weights, cfg.default_source_weight)
    if rule is not None:
        ctype, src = rule[0], "rule"
    elif story.story_id in inp.llm_types:
        ctype, d_llm = inp.llm_types[story.story_id]
        src = "model"
        if dirn == "mixed" and d_llm in ("positive", "negative"):
            dirn = d_llm
    else:
        ctype, src = "OTHER", "none"
    s = inp.series.get(story.ticker)
    i0 = day0_index(s, story.first_seen) if s is not None else None
    beta = beta_at(s, inp.spy, i0) if s is not None and i0 is not None else 1.0
    rx = reaction(s, inp.spy, i0, beta) if s is not None and i0 is not None else None
    last = float(s.c[i0]) if s is not None and i0 is not None else 0.0
    soc = None
    if i0 is not None and s is not None:
        d0 = s.dates[i0]
        soc = next((x for x in reversed(inp.social.get(story.ticker, [])) if x.day <= d0), None)
    agree = agreement(dirn, soc)
    social_pts = 0.0
    if agree == "AGREES" and soc is not None and soc.mention_z is not None:
        social_pts = 5 * min(1.0, soc.mention_z / 3)
    iv = inp.iv.get(story.ticker) or {}
    implied_1d = float(iv["atm_iv_30d"]) / np.sqrt(252) if iv.get("atm_iv_30d") else None
    st = score(story, ctype, sent, weights=cfg.source_weights,
               default_w=cfg.default_source_weight, reaction=rx, direction=dirn,
               surprise=_surprise(ctype, story, inp, last), social_points=social_pts,
               implied_1d=implied_1d)
    crowd = None
    if s is not None and i0 is not None and i0 >= 20:
        atr = ta.atr(s.h[: i0 + 1], s.l[: i0 + 1], s.c[: i0 + 1], 14)[-1]
        ema20 = ta.ema(s.c[: i0 + 1], 20)[-1]
        ext = float((s.c[i0] - ema20) / atr) if atr and not np.isnan(atr) else None
        crowd = crowded(soc, ext)
    return Typed(story, ctype, src, dirn, sent, st, i0, rx, beta, soc, agree, crowd)


def _trend_agrees(s: Series, i: int, dirn: str) -> bool:
    if i < 50 or dirn == "mixed":
        return False
    up = float(s.c[i]) > float(np.mean(s.c[i - 49:i + 1]))
    return up == (dirn == "positive")


def flags_for(t: Typed, s: Series) -> dict[str, bool]:
    """NC-20 flags, identical for past events and live outlooks."""
    i = t.i0 if t.i0 is not None else len(s.c) - 1
    return {"pead": t.ctype == "EARNINGS" and t.direction != "mixed",
            "priced_in": t.strength.priced_in,
            "crowding": t.crowding is not None,
            "trend": _trend_agrees(s, i, t.direction)}


def best_per_day(typed: Sequence[Typed]) -> list[Typed]:
    """One event per ticker and day 0 (the strongest): many stories on one day are one event
    for the price, and counting them twice would overstate the evidence."""
    best: dict[tuple[str, int], Typed] = {}
    for t in typed:
        if t.i0 is None:
            continue
        k = (t.story.ticker, t.i0)
        if k not in best or t.strength.score > best[k].strength.score:
            best[k] = t
    return sorted(best.values(), key=lambda x: (x.story.first_seen, x.story.ticker))


def event_obs(typed: Sequence[Typed], inp: Inputs, horizons: Sequence[int],
              min_strength: float = 0.0) -> list[EventObs]:
    """NC-19 / NC-30: past stories with known outcomes, in volatility units."""
    out = []
    for t in best_per_day(typed):
        s = inp.series.get(t.story.ticker)
        if s is None or t.i0 is None or t.strength.score < min_strength:
            continue
        got = observe(s, inp.spy, t.i0, t.beta, horizons)
        if got is None:
            continue
        xa, xr, end, sig = got
        out.append(EventObs(ticker=t.story.ticker, d0=s.dates[t.i0], ctype=t.ctype,
                            direction=t.direction, strength=t.strength.score, sigma=sig,
                            x_abn=xa, x_raw=xr, end=end, flags=flags_for(t, s)))
    return out


def outlook(t: Typed, pool: Pool, inp: Inputs, cfg: CatalystCfg, now: datetime, *,
            adjustments: Sequence[str] = (), calibrated: bool = False,
            sizer: Callable[[float, float], tuple[int, float, str]] | None = None
            ) -> Outlook | None:
    """NC-16..NC-26 for one typed story, as of the latest bar in its series."""
    tk = t.story.ticker
    s = inp.series.get(tk)
    if s is None or t.i0 is None:
        return None
    i = len(s.c) - 1                       # as of the latest close
    sig = blended_sigma(ewma_sigma(s.c, i + 1), (inp.iv.get(tk) or {}).get("atm_iv_30d"),
                        cfg.iv_weight)
    if sig is None:
        return None
    probe = EventObs(ticker=tk, d0=s.dates[t.i0], ctype=t.ctype, direction=t.direction,
                     strength=t.strength.score, sigma=sig, flags=flags_for(t, s))
    e_next = inp.earnings_next.get(tk)
    views = []
    for h in cfg.horizons:
        ev = 0.0
        if e_next is not None and 0 < e_next <= h and t.ctype != "EARNINGS":
            ev += (2.5 * sig) ** 2            # an earnings day moves ~2.5 daily sigmas
        ev += sum((1.0 * sig) ** 2 for off, _ in inp.macro if 0 < off <= h)
        views.append(horizon_view(pool, probe, h, sig, k=cfg.shrink_k,
                                  adjustments=adjustments, event_var=ev,
                                  calibrated=calibrated))
    last = float(s.c[i])
    i0 = t.i0
    checks = (cx.day0(s, i0, t.direction) + cx.technical(s, inp.spy, i, t.direction)
              + cx.options(inp.iv.get(tk))
              + cx.analysts(inp.actions.get(tk, []),
                            (inp.fundamentals.get(tk) or {}).get("recommendation"), last,
                            t.direction)
              + cx.earnings((inp.fundamentals.get(tk) or {}).get("earnings"), e_next,
                            t.direction)
              + cx.insiders((inp.fundamentals.get(tk) or {}).get("insider"), t.direction)
              + cx.short_interest((inp.fundamentals.get(tk) or {}).get("short_volume"),
                                  t.direction))
    spy_i = inp.spy.dates.index(s.dates[i]) if inp.spy is not None and s.dates[i] in \
        inp.spy.dates else None
    checks += cx.regime(inp.spy, inp.sector.get(tk), spy_i,
                        [n for off, n in inp.macro if off <= max(cfg.horizons)], t.direction)
    st, v, why = stance(views, strength_label=t.strength.label, stance_p=cfg.stance_p,
                        min_edge=cfg.stance_min_edge, earnings_in=e_next,
                        is_earnings=t.ctype == "EARNINGS")
    rx_agrees = None
    if t.reaction is not None and t.direction != "mixed":
        rx_agrees = (t.reaction.abnormal > 0) == (t.direction == "positive")
    iv_rank = (inp.iv.get(tk) or {}).get("iv_rank")
    veh = vehicle(st, iv_rank, rx_agrees)
    tr = ta.atr(s.h, s.l, s.c, 14)[-1]
    atr = float(tr) if not np.isnan(tr) else 0.0
    pl = plan(st, v, day0_high=float(s.h[i0]), day0_low=float(s.l[i0]), atr=atr, last=last,
              sizer=sizer) if veh != "wait" else {}
    if veh == "wait":
        why += "; the day-0 reaction disagrees with the news, wait for it to confirm"
    oid = "co-" + hashlib.sha1(f"{t.story.story_id}|{s.dates[i]}".encode(),
                               usedforsecurity=False).hexdigest()[:20]
    return Outlook(outlook_id=oid, ticker=tk, as_of=s.dates[i], typed=t,
                   strength_now=decayed(t.strength.score, t.story.last_seen, now,
                                        cfg.half_life_h),
                   views=views, confluence=checks,
                   view=View(st, v.h if v else None, veh, why, pl),
                   scenarios=scenarios(tk, t.direction, float(s.h[i0]), float(s.l[i0]), views),
                   calibrated=calibrated, last=last,
                   day0={"date": s.dates[i0].isoformat(), "high": float(s.h[i0]),
                         "low": float(s.l[i0]), "close": float(s.c[i0])})


def live(typed: Sequence[Typed], cfg: CatalystCfg, now: datetime) -> list[Typed]:
    """NC-4: stories that count as a catalyst now (strength after decay, or a hard event in
    the story window), strongest first, one per ticker."""
    best: dict[str, Typed] = {}
    for t in typed:
        age_h = (now - t.story.last_seen).total_seconds() / 3600
        if age_h > cfg.story_window_h:
            continue
        now_score = decayed(t.strength.score, t.story.last_seen, now, cfg.half_life_h)
        if now_score < cfg.min_strength and not t.strength.hard_event:
            continue
        tk = t.story.ticker
        if tk not in best or t.strength.score > best[tk].strength.score:
            best[tk] = t
    return sorted(best.values(), key=lambda x: -x.strength.score)
