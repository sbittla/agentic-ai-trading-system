"""A4 Technical analysis agent (A4-1..A4-4, STRATEGY_SPEC.md §1-§5).

Computes the 8:00 AM level map, evaluates the market/stock filters (MF-1..MF-5) and detects
only the playbook setups, each with trigger, structural stop, targets and a 0-100 score.
Rules are evaluated on closed bars only (CK-5), so replay and live make the same decisions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import structlog

from trader.agents.a02_market_data import MarketDataAgent, SymbolState
from trader.calendar import MacroCalendar, MarketCalendar
from trader.config import Config
from trader.indicators import atr_series, day_range_position, ema_series, gap_pct
from trader.models.messages import Bar, EventFlags, Levels, SetupTriggered
from trader.timeutil import cents, cents_down, minutes_since_open, to_et

log = structlog.get_logger(__name__)
BELOW_HOD_SETUPS = ("VWAP_RECLAIM", "FIRST_PULLBACK", "VWAP_TREND")


def compute_levels(ticker: str, day: date, daily: list[Bar], pm_high: float | None = None,
                   pm_low: float | None = None) -> Levels | None:
    """A4-1 key levels from daily bars strictly before ``day`` (no look-ahead, BT-2)."""
    prior = [b for b in daily if to_et(b.ts).date() < day]
    if not prior:
        return None
    closes = [b.c for b in prior]
    highs = [b.h for b in prior]
    lows = [b.l for b in prior]
    e20 = ema_series(closes, 20)
    e50 = ema_series(closes, 50)
    e10 = ema_series(closes, 10)
    atr = atr_series(highs, lows, closes, 14)
    last5 = range(max(0, len(prior) - 5), len(prior))
    pullback = False
    for i in last5:
        for e in (e20[i], e50[i]):
            if e is not None and lows[i] <= e * 1.005:
                pullback = True
    last = prior[-1]
    vols = [b.v for b in prior[-50:]]
    gaps = [abs(prior[i].o / prior[i - 1].c - 1) * 100
            for i in range(max(1, len(prior) - 20), len(prior)) if prior[i - 1].c]
    return Levels(day=day, ticker=ticker, prior_high=last.h, prior_low=last.l,
                  prior_close=last.c, pm_high=pm_high, pm_low=pm_low, ema20d=e20[-1],
                  ema50d=e50[-1], ema10d=e10[-1], atr14d=atr[-1],
                  high20d=max(highs[-20:]), close_high20d=max(closes[-20:]),
                  avg_vol50d=sum(vols) / len(vols) if vols else None,
                  low5d=min(lows[-5:]), pullback_to_ema=pullback, history_days=len(prior),
                  avg_gap20d=sum(gaps) / len(gaps) if gaps else None)


def classify_regime(spy_daily: list[Bar], day: date, spy_gap_pct: float | None,
                    high_vol_atr_ratio: float, high_vol_gap_pct: float
                    ) -> tuple[str, float | None, str]:
    """RG-1: ``high-vol`` when SPY's ATR(14-day) is above ``high_vol_atr_ratio`` x its 60-day
    average, or the opening gap is at least ``high_vol_gap_pct``; else ``normal``."""
    prior = [b for b in spy_daily if to_et(b.ts).date() < day]
    atr = [a for a in atr_series([b.h for b in prior], [b.l for b in prior],
                                 [b.c for b in prior], 14) if a is not None]
    ratio = None
    if len(atr) >= 20:
        window = atr[-61:-1] if len(atr) > 60 else atr[:-1]
        avg = sum(window) / len(window)
        ratio = atr[-1] / avg if avg > 0 else None
    reasons = []
    if ratio is not None and ratio > high_vol_atr_ratio:
        reasons.append(f"SPY ATR14 {ratio:.2f}x its 60-day average")
    if spy_gap_pct is not None and abs(spy_gap_pct) >= high_vol_gap_pct:
        reasons.append(f"SPY gap {spy_gap_pct:+.2f}%")
    return ("high-vol" if reasons else "normal"), ratio, "; ".join(reasons) or "calm"


def session_screen(daily: list[Bar], lv: Levels) -> dict[str, Any]:
    """Next-session screen from the latest completed session (A5-1 preview plan).

    ``lv`` must come from ``compute_levels`` for the session being planned, so its prior_*
    fields describe the latest completed session. Every number the plan may use as a trigger,
    stop or target is computed here, so the plan passes the PR-4 numbers check. Morning
    triggers are only reference levels: the live ORB/GAP triggers come from the opening range.
    """
    prior = [b for b in daily if to_et(b.ts).date() < lv.day]
    last = prior[-1]
    prev_close = prior[-2].c if len(prior) > 1 else last.c
    atr = lv.atr14d or 0.0
    rng = last.h - last.l
    range_pos = (last.c - last.l) / rng if rng > 0 else 0.5
    change = (last.c / prev_close - 1) * 100 if prev_close else 0.0
    vol_ratio = last.v / lv.avg_vol50d if lv.avg_vol50d else None
    trend_up = bool(lv.ema20d and lv.ema50d and last.c > lv.ema20d > lv.ema50d)
    # the 20-day closing high before the latest session: the base the breakout must clear
    base = max(b.c for b in prior[-21:-1]) if len(prior) > 1 else last.c

    def dist(level: float | None) -> float | None:
        return round((level - last.c) / atr, 2) if level is not None and atr else None

    def _up(v: float) -> float:
        return float(cents(v))

    def _down(v: float) -> float:
        return float(cents_down(v))

    cands: list[dict[str, Any]] = []
    if atr:
        if trend_up and range_pos >= 0.5:
            stop = max(last.l, last.h - 0.5 * atr)
            cands.append({"setup": "ORB", "trigger": _up(last.h), "stop": _down(stop),
                          "target_1": _up(2 * last.h - stop),
                          "basis": "uptrend, strong close: reference trigger = prior day high "
                                   "(live ORB trigger comes from the opening range)"})
        if trend_up and lv.pullback_to_ema:
            swing_stop = lv.low5d - 0.1 * atr if lv.low5d is not None else None
            if swing_stop is not None and last.h - swing_stop <= 1.5 * atr:
                cands.append({"setup": "SWING_CLOSE_NEAR_HIGH", "trigger": _up(last.h),
                              "stop": _down(swing_stop),
                              "target_1": _up(last.h + 1.5 * (last.h - swing_stop)),
                              "basis": "uptrend with a pullback to the 20/50-day EMA in the "
                                       "last 5 sessions; needs a close near the high"})
        if (trend_up and 0 <= base - last.c <= 1.0 * atr) or last.c > base:
            level = max(base, lv.close_high20d or base) if last.c <= base else last.h
            stop = level - 0.5 * atr
            cands.append({"setup": "SWING_BASE_BREAKOUT", "trigger": _up(level),
                          "stop": _down(stop), "target_1": _up(level + 0.75 * atr),
                          "basis": "closed above the 20-day closing high" if last.c > base
                          else "within 1 ATR of the 20-day closing high"})
    score = (30 * trend_up + 20 * (range_pos >= 0.75 and change >= 0)
             + 15 * bool(vol_ratio and vol_ratio >= 1.0) + 15 * bool(lv.pullback_to_ema)
             + 20 * bool(cands))
    return {"session_date": to_et(last.ts).date().isoformat(), "close": last.c,
            "change_pct": round(change, 2), "range_pos": round(range_pos, 2),
            "volume_vs_50d_avg": round(vol_ratio, 2) if vol_ratio else None,
            "trend_up": trend_up, "atr_to_prior_high": dist(last.h),
            "atr_to_20d_closing_high": dist(base), "atr_to_20d_high": dist(lv.high20d),
            "atr_to_ema20d": dist(lv.ema20d), "readiness_score": score,
            "candidates": cands}


@dataclass
class Armed:
    """A setup whose trigger is a break of a level by a later 1-min trade."""

    setup: str
    window: str
    level: float
    stop_level: float      # structural level before the buffer
    armed_at: datetime
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TickerDay:
    """One-shot flags and armed setups for one ticker on one day."""

    orb_done: bool = False
    gap_done: bool = False
    pullback_done: bool = False
    reclaim_below_seen: bool = False
    reclaim_dip_low: float | None = None
    reclaim_done: bool = False
    power_done: bool = False
    trend_done: bool = False
    swing_done: bool = False
    armed: dict[str, Armed] = field(default_factory=dict)


def _t(dt: datetime) -> time:
    return to_et(dt).time()


class TechnicalAgent:
    def __init__(self, cfg: Config, md: MarketDataAgent, calendar: MarketCalendar,
                 macro: MacroCalendar, *, require_quotes: bool = True) -> None:
        self.cfg = cfg
        self.md = md
        self.cal = calendar
        self.macro = macro
        self.require_quotes = require_quotes
        self.levels: dict[str, Levels] = {}
        self.news: dict[str, EventFlags] = {}
        self.news_available = True
        self.day: date | None = None
        self.days: dict[str, TickerDay] = {}
        self.regime = "normal"   # RG-1, set by the engine at 8:30 and 9:30
        self.passive_setups: set[str] = set()   # EXQ-1: limit at the trigger

    # -- lifecycle ---------------------------------------------------------------------------
    def start_day(self, d: date) -> None:
        self.day = d
        self.days = {t: TickerDay() for t in self.cfg.universe.tickers}

    def set_levels(self, lv: Levels) -> None:
        self.levels[lv.ticker] = lv

    def refresh_premarket(self) -> None:
        """Attach pre-market high/low (from A2) to today's levels at 9:30."""
        for t, lv in list(self.levels.items()):
            st = self.md.state.get(t)
            if st is not None and st.pm_high is not None:
                self.levels[t] = lv.model_copy(update={"pm_high": st.pm_high,
                                                       "pm_low": st.pm_low})

    # -- helpers -----------------------------------------------------------------------------
    def trend_day(self, st: SymbolState) -> bool:
        """RG-3 with the configured thresholds."""
        rg = self.cfg.regime
        hh, mm = (int(x) for x in rg.trend_day_high_after.split(":"))
        return st.trend_day(rg.trend_day_vwap_pct, time(hh, mm))

    def _buffer(self, atr5: float) -> float:
        return 0.05 * atr5

    def entry_price(self, trigger: float, atr5: float, setup: str | None = None) -> Decimal:
        """STRATEGY_SPEC §2: limit at trigger + 0.10 ATR5 + marketable offset, never above the
        chase limit trigger + 0.25 ATR5 (CK-6). EXQ-1: a setup with high slippage enters with
        a passive limit at the trigger."""
        if setup is not None and setup in self.passive_setups:
            return cents(trigger)
        e = self.cfg.entries
        raw = trigger + 0.10 * atr5 + trigger * e.limit_offset_pct / 100
        cap = trigger + e.max_chase_atr5 * atr5
        return cents(min(raw, cap))

    def resistance(self, ticker: str, entry: float, st: SymbolState, include_hod: bool = True
                   ) -> float | None:
        """Nearest of prior day high, pre-market high, 20-day high and - only for setups that
        trigger below the high of day - HOD (STRATEGY_SPEC §1). A breakout's own bar is not
        resistance."""
        lv = self.levels.get(ticker)
        cands: list[float] = []
        if lv is not None:
            cands += [lv.prior_high]
            if lv.pm_high is not None:
                cands.append(lv.pm_high)
            if lv.high20d is not None:
                cands.append(lv.high20d)
        if include_hod and st.hod is not None:
            cands.append(st.hod)
        above = [c for c in cands if c > entry]
        return min(above) if above else None

    def rel_strength(self, st: SymbolState) -> float | None:
        q = self.md.state.get("QQQ")
        a = st.change_from_open_pct()
        b = q.change_from_open_pct() if q else None
        return None if a is None or b is None else a - b

    def score(self, ticker: str, st: SymbolState, entry: float, r: float,
              resistance: float | None) -> int:
        """Composite 0-100 signal score (adapted from the alert bot, ALERT_BOT_REUSE)."""
        s = 0.0
        rv = st.rvol() or 0.0
        s += min(rv / 3.0, 1.0) * 30
        rs = self.rel_strength(st)
        if rs is not None:
            s += max(0.0, min((rs + 1.0) / 2.0, 1.0)) * 20
        room = (resistance - entry) / r if resistance is not None and r > 0 else 3.0
        s += max(0.0, min(room / 3.0, 1.0)) * 20
        if st.above_vwap():
            s += 10
        if self.trend_day(st):
            s += 5
        if (st.ema9_5m.value or 0) > (st.ema20_5m.value or 0):
            s += 5
        nf = self.news.get(ticker)
        tone = nf.tone if nf is not None else "neutral"
        s += {"positive": 10, "neutral": 5, "mixed": 5, "negative": 0}[tone]
        return int(round(max(0.0, min(s, 100.0))))

    def quote_reliable(self, ticker: str, now: datetime) -> bool:
        """DQ-8 / MF-6: two-sided, not crossed, and at most ``data.quote_max_age_s`` old."""
        q = self.md.state[ticker].quote
        if q is None:
            return not self.require_quotes
        age = (now - q.ts).total_seconds()
        return (q.bid > 0 and q.ask > 0 and q.ask >= q.bid
                and -1.0 <= age <= self.cfg.data.quote_max_age_s)

    def filters(self, ticker: str, now: datetime, setup: str, score: int) -> dict[str, bool]:
        """MF-1..MF-6 pass/fail (A4-4). MF-6: the spread check needs a reliable quote."""
        f = self.cfg.filters
        st = self.md.state[ticker]
        qqq = self.md.state.get("QQQ")
        qqq_above = qqq.above_vwap() if qqq else None
        qqq_chg = qqq.day_change_pct() if qqq else None
        if qqq_chg is None and qqq is not None:
            qqq_chg = qqq.change_from_open_pct()
        qqq_ok = (qqq_above is True or not f.qqq_above_vwap_required) and (
            qqq_chg is not None and qqq_chg > -f.qqq_max_day_drop_pct)
        if not qqq_ok and setup != "VWAP_RECLAIM":
            # MF-1 exception: A+ setup with relative strength (stock above VWAP, QQQ below)
            rs = self.rel_strength(st)
            qqq_ok = bool(st.above_vwap() and qqq_above is False and rs is not None and rs > 0
                          and score >= 80 and (qqq_chg is None
                                               or qqq_chg > -f.qqq_max_day_drop_pct))
        mf2 = True
        if ticker in f.smh_check_for:
            smh = self.md.state.get("SMH")
            mf2 = bool(smh and smh.above_vwap())
        rv = st.rvol()
        mf3 = rv is not None and rv >= f.min_rvol
        mf6 = self.quote_reliable(ticker, now)
        if st.quote is not None:
            mf4 = mf6 and st.quote.spread_pct < f.max_spread_pct   # DQ-8: never by default
        else:
            mf4 = not self.require_quotes
        mf5 = not (self.md.recent_halt(ticker, now, f.halt_lookback_min)
                   or self.md.is_stale(now) or st.incomplete)
        return {"MF-1": bool(qqq_ok), "MF-2": mf2, "MF-3": mf3, "MF-4": mf4, "MF-5": mf5,
                "MF-6": mf6}

    def _setup_id(self, ticker: str, setup: str, ts: datetime) -> str:
        key = f"{ticker}|{setup}|{ts.isoformat()}"
        return hashlib.sha256(key.encode()).hexdigest()[:24]

    def _emit(self, *, ticker: str, setup: str, window: str, style: str, now: datetime,
              trigger: float, stop_level: float, st: SymbolState, t1_r: float, t2_r: float,
              t2_floor: float | None = None, buffer: float | None = None,
              context: dict[str, Any] | None = None) -> SetupTriggered | None:
        atr5 = st.atr5.value
        if atr5 is None or atr5 <= 0:
            return None
        entry = self.entry_price(trigger, atr5, setup)
        b = self._buffer(atr5) if buffer is None else buffer
        stop = cents_down(stop_level - b)
        r = entry - stop
        if r <= 0:
            return None
        t1 = cents(entry + Decimal(str(t1_r)) * r)
        t2 = cents(entry + Decimal(str(t2_r)) * r)
        if t2_floor is not None and Decimal(str(t2_floor)) > t2:
            t2 = cents(t2_floor)
        res = self.resistance(ticker, float(entry), st, include_hod=setup in BELOW_HOD_SETUPS)
        sc = self.score(ticker, st, float(entry), float(r), res)
        ctx = {"atr14d": (self.levels[ticker].atr14d if ticker in self.levels else None),
               "hod": st.hod, "ema9_5m": st.ema9_5m.value, "ema20_5m": st.ema20_5m.value,
               "or5": st.or5, "rel_strength": self.rel_strength(st),
               "trend_day": self.trend_day(st), "regime": self.regime,
               "breakout_level": trigger, **(context or {})}
        return SetupTriggered(
            setup_id=self._setup_id(ticker, setup, now), ts=now, ticker=ticker,
            style="swing" if style == "swing" else "day",
            window="morning" if window == "morning" else "afternoon", setup=setup,
            trigger=cents(trigger), entry=entry, stop=stop, t1=t1, t2=t2, score=sc, atr5=atr5,
            vwap=st.vwap_value or 0.0, rvol=st.rvol() or 0.0, last_price=st.last_price or 0.0,
            resistance=res, filters=self.filters(ticker, now, setup, sc), context=ctx)

    # -- 5-minute closes ---------------------------------------------------------------------
    def on_5m_close(self, bar: Bar, now: datetime) -> list[SetupTriggered]:
        t = bar.symbol
        if t not in self.days:
            return []
        st = self.md.state[t]
        td = self.days[t]
        end = _t(bar.end)
        out: list[SetupTriggered | None] = []
        if time(9, 40) <= end <= time(10, 15):
            out.append(self._orb(t, st, td, bar, now))
            out.append(self._gap_and_go(t, st, td, bar, now))
        if time(9, 35) < end <= time(10, 15):
            self._track_reclaim(t, st, td, bar, now)
            self._track_pullback(st, td, bar, now)
        if time(15, 5) <= end <= time(15, 40):
            out.append(self._power_hour(t, st, td, bar, now))
            self._track_vwap_trend(st, td, bar, now)
        if end == time(15, 45):
            out += self._swing(t, st, td, now)
        return [s for s in out if s is not None]

    def on_1m_close(self, bar: Bar, now: datetime) -> list[SetupTriggered]:
        """Armed setups fire on a closed 1-min bar trading above the level."""
        t = bar.symbol
        td = self.days.get(t)
        if td is None or not td.armed:
            return []
        st = self.md.state[t]
        out: list[SetupTriggered] = []
        end = _t(bar.end)
        for name, arm in list(td.armed.items()):
            in_window = (time(9, 45) <= end <= time(10, 15)) if arm.window == "morning" else (
                time(15, 5) <= end <= time(15, 40))
            if not in_window:
                if end > (time(10, 15) if arm.window == "morning" else time(15, 40)):
                    del td.armed[name]
                continue
            if bar.h > arm.level:
                del td.armed[name]
                s = self._fire_armed(t, st, arm, now)
                if s is not None:
                    out.append(s)
        return out

    def _fire_armed(self, t: str, st: SymbolState, arm: Armed, now: datetime
                    ) -> SetupTriggered | None:
        if arm.setup == "VWAP_RECLAIM":
            return self._emit(ticker=t, setup="VWAP_RECLAIM", window="morning", style="day",
                              now=now, trigger=arm.level, stop_level=arm.stop_level, st=st,
                              t1_r=self.cfg.exits.day.t1_r, t2_r=2.0)
        if arm.setup == "FIRST_PULLBACK":
            return self._emit(ticker=t, setup="FIRST_PULLBACK", window="morning", style="day",
                              now=now, trigger=arm.level, stop_level=arm.stop_level, st=st,
                              t1_r=self.cfg.exits.day.t1_r, t2_r=2.0, t2_floor=st.hod)
        if arm.setup == "VWAP_TREND":
            atr5 = st.atr5.value or 0.0
            return self._emit(ticker=t, setup="VWAP_TREND", window="afternoon", style="day",
                              now=now, trigger=arm.level, stop_level=arm.stop_level, st=st,
                              t1_r=self.cfg.exits.day.t1_r, t2_r=2.0, t2_floor=st.hod,
                              buffer=0.25 * atr5)
        return None

    # -- morning setups ----------------------------------------------------------------------
    def _orb(self, t: str, st: SymbolState, td: TickerDay, bar: Bar, now: datetime
             ) -> SetupTriggered | None:
        if td.orb_done or st.or5 is None or st.atr5.value is None:
            return None
        atr5 = st.atr5.value
        or_h, or_l = st.or5
        avg_vol = st.or5_avg_vol
        if or_h - or_l < 0.3 * atr5:   # too narrow: use OR15 from the 9:50 bar
            if _t(bar.end) < time(9, 50) or st.or15 is None:
                return None
            or_h, or_l = st.or15
            avg_vol = st.or15_avg_vol
        if not (bar.c > or_h and avg_vol is not None and bar.v >= avg_vol):
            return None
        td.orb_done = True   # one-shot: first trigger only
        lv = self.levels.get(t)
        if lv is not None and st.open_price is not None and gap_pct(st.open_price,
                                                                    lv.prior_close) < -1.0:
            log.info("orb_skip_gap_down", ticker=t)
            return None
        vw = st.vwap_value
        if vw is not None and bar.c < vw:
            log.info("orb_skip_below_vwap", ticker=t)
            return None
        entry = float(self.entry_price(or_h, atr5))
        stop_level = or_l
        if entry - (or_l - self._buffer(atr5)) > atr5:
            stop_level = (or_h + or_l) / 2
        return self._emit(ticker=t, setup="ORB", window="morning", style="day", now=now,
                          trigger=or_h, stop_level=stop_level, st=st,
                          t1_r=self.cfg.exits.day.t1_r, t2_r=2.0,
                          context={"or_high": or_h, "or_low": or_l})

    def _gap_and_go(self, t: str, st: SymbolState, td: TickerDay, bar: Bar, now: datetime
                    ) -> SetupTriggered | None:
        if td.gap_done or _t(bar.end) > time(10, 0) or st.or5 is None:
            return None
        lv = self.levels.get(t)
        if lv is None or lv.atr14d is None or st.open_price is None:
            return None
        g = gap_pct(st.open_price, lv.prior_close)
        atr_pct = lv.atr14d / lv.prior_close * 100
        if not 1.0 <= g <= min(3 * atr_pct, 6.0):
            return None
        rg = self.cfg.regime
        if self.regime == "high-vol" and lv.avg_gap20d and g > rg.gap_and_go_max_normal_x * \
                lv.avg_gap20d:   # RG-2: no gap-and-go on an outsized gap in a high-vol regime
            log.info("gap_skip_high_vol", ticker=t, gap=g, normal=lv.avg_gap20d)
            return None
        news = self.news.get(t)
        if not self.news_available or news is None or news.catalyst.lower() in ("", "none"):
            return None
        pm = st.pm_high if st.pm_bars > 0 else None
        name = "GAP_AND_GO" if pm is not None else "GAP_AND_GO_NO_PM"
        level = max(pm, st.or5[0]) if pm is not None else st.or5[0]
        if bar.c <= level:
            return None
        td.gap_done = True
        return self._emit(ticker=t, setup=name, window="morning", style="day", now=now,
                          trigger=level, stop_level=st.or5[1], st=st,
                          t1_r=self.cfg.exits.day.t1_r, t2_r=2.0,
                          context={"gap_pct": g, "catalyst": news.catalyst})

    def _track_reclaim(self, t: str, st: SymbolState, td: TickerDay, bar: Bar,
                       now: datetime) -> None:
        if td.reclaim_done:
            return
        vw = st.vwap_value
        if vw is None:
            return
        if bar.c < vw:
            td.reclaim_below_seen = True
            td.armed.pop("VWAP_RECLAIM", None)
        if td.reclaim_below_seen:
            td.reclaim_dip_low = bar.l if td.reclaim_dip_low is None else min(
                td.reclaim_dip_low, bar.l)
        if not td.reclaim_below_seen or _t(bar.end) < time(9, 45):
            return
        prev = st.bars5[-2] if len(st.bars5) >= 2 else None
        if bar.c > vw and prev is not None and bar.v > prev.v and td.reclaim_dip_low is not None:
            qqq = self.md.state.get("QQQ")
            if qqq is None or not qqq.above_vwap():   # MF-1, no exception for this setup
                return
            td.armed["VWAP_RECLAIM"] = Armed("VWAP_RECLAIM", "morning", bar.h,
                                             td.reclaim_dip_low, now)
            td.reclaim_done = True

    def _track_pullback(self, st: SymbolState, td: TickerDay, bar: Bar, now: datetime
                        ) -> None:
        if td.pullback_done or st.or5 is None or st.atr5.value is None or st.hod is None:
            return
        atr5 = st.atr5.value
        if _t(bar.end) < time(9, 45):
            return
        if st.hod < st.or5[0] + 0.5 * atr5 or (st.rvol_at_hod or 0) < 2.0:
            return
        bars = st.bars5
        hod_idx = max(range(len(bars)), key=lambda i: bars[i].h)
        seq = bars[hod_idx + 1:]
        if not seq:
            return
        if len(seq) > 3:
            td.pullback_done = True
            td.armed.pop("FIRST_PULLBACK", None)
            return
        prev_high = bars[hod_idx].h
        vw = st.vwap_value or 0.0
        ema9 = st.ema9_5m.value if st.ema9_5m.value is not None else vw
        floor = min(vw, ema9) - 0.1 * atr5
        for b in seq:
            if b.h >= prev_high or b.l < floor:
                td.pullback_done = True   # first pullback failed: no retry
                td.armed.pop("FIRST_PULLBACK", None)
                return
            prev_high = b.h
        td.armed["FIRST_PULLBACK"] = Armed("FIRST_PULLBACK", "morning", seq[-1].h,
                                           min(b.l for b in seq), now)

    # -- afternoon setups --------------------------------------------------------------------
    def _power_hour(self, t: str, st: SymbolState, td: TickerDay, bar: Bar, now: datetime
                    ) -> SetupTriggered | None:
        if td.power_done or st.hod_before_1500 is None or len(st.bars5) < 3:
            return None
        if not self.trend_day(st) or not st.above_vwap():   # RG-3
            return None
        if bar.c <= st.hod_before_1500:
            return None
        td.power_done = True
        stop_level = min(b.l for b in st.bars5[-3:])
        return self._emit(ticker=t, setup="POWER_HOUR_BREAKOUT", window="afternoon",
                          style="day", now=now, trigger=st.hod_before_1500,
                          stop_level=stop_level, st=st, t1_r=self.cfg.exits.day.t1_r, t2_r=2.0)

    def _track_vwap_trend(self, st: SymbolState, td: TickerDay, bar: Bar, now: datetime
                          ) -> None:
        if td.trend_done or st.atr5.value is None:
            return
        vw = st.vwap_value
        if vw is None or not self.trend_day(st):   # RG-3
            return
        for b, v in zip(st.bars5, st.vwap_at_5m, strict=True):
            if _t(b.end) > time(14, 0) and b.c < v:
                td.armed.pop("VWAP_TREND", None)
                return
        if bar.l <= vw + 0.1 * st.atr5.value and bar.c > vw:
            td.armed["VWAP_TREND"] = Armed("VWAP_TREND", "afternoon", bar.h, vw, now)
            td.trend_done = True

    def _swing(self, t: str, st: SymbolState, td: TickerDay, now: datetime
               ) -> list[SetupTriggered | None]:
        if td.swing_done:
            return []
        td.swing_done = True
        lv = self.levels.get(t)
        if lv is None or lv.atr14d is None or st.last_price is None or st.lod is None:
            return []
        news = self.news.get(t)
        flags = set(news.flags) if news else set()
        d = to_et(now).date()
        if flags & {"EARNINGS_TODAY", "EARNINGS_3D"}:
            return []
        if self.macro.is_fomc(self.cal.next_trading_day(d)):
            return []
        last = st.last_price
        out: list[SetupTriggered | None] = []
        atr14 = lv.atr14d
        ex = self.cfg.exits.swing
        # SWING_CLOSE_NEAR_HIGH (§5.3)
        if (lv.ema20d is not None and lv.ema50d is not None and last > lv.ema20d > lv.ema50d
                and (lv.pullback_to_ema or st.lod <= lv.ema20d * 1.005)
                and day_range_position(last, st.lod, st.hod or last) >= 0.75
                and last >= lv.prior_close):
            low5 = min(lv.low5d if lv.low5d is not None else st.lod, st.lod)
            stop_level = low5 - 0.1 * atr14
            if last - stop_level <= 1.5 * atr14:
                out.append(self._emit(ticker=t, setup="SWING_CLOSE_NEAR_HIGH",
                                      window="afternoon", style="swing", now=now, trigger=last,
                                      stop_level=stop_level, st=st, t1_r=ex.t1_r, t2_r=3.0,
                                      buffer=0.0))
        # SWING_BASE_BREAKOUT (§5.4)
        if lv.close_high20d is not None and lv.avg_vol50d and last > lv.close_high20d:
            minute = minutes_since_open(now) - 1
            frac = st.rvol_profile.fraction_done(minute) or 0.9
            projected = st.cum_vol / frac if frac > 0 else 0.0
            if projected >= lv.avg_vol50d:
                out.append(self._emit(ticker=t, setup="SWING_BASE_BREAKOUT",
                                      window="afternoon", style="swing", now=now, trigger=last,
                                      stop_level=lv.close_high20d - 0.5 * atr14, st=st,
                                      t1_r=ex.t1_r, t2_r=3.0, buffer=0.0,
                                      context={"projected_volume": projected,
                                               "breakout_level": lv.close_high20d}))
        return out
