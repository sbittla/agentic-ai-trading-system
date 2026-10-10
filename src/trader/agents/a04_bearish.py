"""Bearish setups for options only (OPTIONS_REQUIREMENTS BR-1..BR-4).

The stock side stays long-only (ADR-008). These signals are ``BearSignal``s, not
``SetupTriggered``s, so they can never enter the stock pipeline (A5..A8); the engine hands
them only to the options desk and journals them with ``direction = bear`` and
``route = options_only`` (BR-4).

BR-1 mirrored setups (5-minute closes, levels in ATR(5m)):
- ORB_BREAKDOWN: a close below the opening-range low (OR15 when OR5 is too narrow).
- VWAP_REJECTION: a failed reclaim (high above VWAP, close below), then a lower high under
  VWAP; triggers on the close below the prior bar's low.
- GAP_DOWN_AND_GO: gap down >= 2% on news, a close below the pre-market low.
- FAILED_BREAKOUT: a new high of day above the opening-range high, then a close back below
  the opening-range high within 15 minutes.
- CLOSE_NEAR_LOW (swing, 3:45 PM): close in the bottom 10% of the range, below VWAP and the
  10-day EMA.

BR-2 mirrored market filter: QQQ below VWAP or relative weakness against QQQ and SMH; RVOL
>= the long threshold; never in the first 5 minutes or after a halt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import structlog

from trader.agents.a02_market_data import MarketDataAgent, SymbolState
from trader.models.messages import Bar
from trader.timeutil import to_et

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class BearSignal:
    setup_id: str
    ts: datetime
    ticker: str
    setup: str
    window: str
    style: str
    trigger: float
    invalidation: float        # the "stop" for the options plan
    t1: float
    t2: float
    score: int
    atr5: float
    rvol: float
    last_price: float
    context: dict[str, Any] = field(default_factory=dict)

    def journal_row(self) -> dict[str, Any]:
        """BR-4: journaled like a long setup."""
        return {"ts": self.ts, "ticker": self.ticker, "style": self.style,
                "window": self.window, "setup_name": self.setup, "trigger": round(self.trigger, 2),
                "entry": round(self.trigger, 2), "stop": round(self.invalidation, 2),
                "t1": round(self.t1, 2), "t2": round(self.t2, 2), "score": self.score,
                "filters_json": {"BR-2": True},
                "context_json": {**self.context, "direction": "bear", "route": "options_only"}}


@dataclass
class BearDay:
    orb_done: bool = False
    gap_done: bool = False
    rejection_done: bool = False
    failed_bo_done: bool = False
    swing_done: bool = False
    failed_reclaim_high: float | None = None
    breakout_at: datetime | None = None


def _t(dt: datetime) -> time:
    return to_et(dt).time()


class BearishScanner:
    def __init__(self, cfg: Any, md: MarketDataAgent, ta: Any) -> None:
        self.cfg = cfg
        self.md = md
        self.ta = ta            # levels and news are shared with A4
        self.days: dict[str, BearDay] = {}
        self.day: date | None = None

    def start_day(self, d: date) -> None:
        self.day = d
        self.days = {t: BearDay() for t in self.cfg.universe.tickers}

    # -- BR-2 ----------------------------------------------------------------------------------
    def market_ok(self, ticker: str, now: datetime) -> tuple[bool, str]:
        st = self.md.state[ticker]
        if _t(now) < time(9, 35):
            return False, "first 5 minutes"
        if st.halted or self.md.recent_halt(ticker, now, self.cfg.filters.halt_lookback_min):
            return False, "halt"
        rv = st.rvol()
        if rv is None or rv < self.cfg.filters.min_rvol:
            return False, f"RVOL {rv or 0:.2f} < {self.cfg.filters.min_rvol}"
        qqq = self.md.state.get("QQQ")
        if qqq is not None and qqq.above_vwap() is False:
            return True, ""
        mine = st.change_from_open_pct()
        others = [x.change_from_open_pct() for x in (qqq, self.md.state.get("SMH"))
                  if x is not None]
        if mine is not None and others and all(o is not None and mine < o for o in others):
            return True, ""
        return False, "QQQ above VWAP and no relative weakness"

    # -- helpers -------------------------------------------------------------------------------
    def _score(self, st: SymbolState) -> int:
        s = 40.0 + min((st.rvol() or 0) / 3.0, 1.0) * 30
        rs = self.ta.rel_strength(st)
        if rs is not None:
            s += max(0.0, min((-rs + 1.0) / 2.0, 1.0)) * 20
        if st.above_vwap() is False:
            s += 10
        return int(round(min(s, 100.0)))

    def _emit(self, t: str, st: SymbolState, setup: str, now: datetime, trigger: float,
              invalidation: float, window: str = "morning", style: str = "day",
              t1_r: float | None = None, t2_r: float = 2.0, **ctx: Any) -> BearSignal | None:
        atr5 = st.atr5.value
        if atr5 is None or atr5 <= 0:
            return None
        if style == "day" and invalidation - trigger > 1.5 * atr5:
            invalidation = trigger + atr5            # mirror of CK-7: max 1 ATR(5m) away
        r = invalidation - trigger
        if r <= 0:
            return None
        t1r = self.cfg.exits.day.t1_r if t1_r is None else t1_r
        key = f"{t}|{setup}|{now.isoformat()}"
        return BearSignal(hashlib.sha256(key.encode()).hexdigest()[:24], now, t, setup, window,
                          style, trigger, invalidation, trigger - t1r * r, trigger - t2_r * r,
                          self._score(st), atr5, st.rvol() or 0.0, st.last_price or trigger,
                          dict(ctx))

    # -- BR-1 ----------------------------------------------------------------------------------
    def on_5m_close(self, bar: Bar, now: datetime) -> list[BearSignal]:
        t = bar.symbol
        bd = self.days.get(t)
        if bd is None:
            return []
        st = self.md.state[t]
        end = _t(bar.end)
        out: list[BearSignal | None] = []
        if time(9, 40) <= end <= time(10, 15):
            out += [self._orb(t, st, bd, bar, now), self._gap_down(t, st, bd, bar, now)]
        if time(9, 35) < end <= time(10, 15) or time(15, 5) <= end <= time(15, 40):
            out.append(self._failed_breakout(t, st, bd, bar, now))
            out.append(self._vwap_rejection(t, st, bd, bar, now, end))
        if end == time(15, 45):
            out.append(self._close_near_low(t, st, bd, bar, now))
        res = []
        for sig in out:
            if sig is None:
                continue
            ok, why = self.market_ok(t, now) if sig.style == "day" else (True, "")
            if ok:
                res.append(sig)
            else:
                log.info("bear_filtered", ticker=t, setup=sig.setup, why=why)
        return res

    def _orb(self, t: str, st: SymbolState, bd: BearDay, bar: Bar, now: datetime
             ) -> BearSignal | None:
        if bd.orb_done or st.or5 is None or st.atr5.value is None:
            return None
        atr5 = st.atr5.value
        or_h, or_l = st.or5
        avg = st.or5_avg_vol
        if or_h - or_l < 0.3 * atr5:
            if _t(bar.end) < time(9, 50) or st.or15 is None:
                return None
            or_h, or_l = st.or15
            avg = st.or15_avg_vol
        if not (bar.c < or_l and avg is not None and bar.v >= avg):
            return None
        bd.orb_done = True
        vw = st.vwap_value
        if vw is not None and bar.c > vw:
            return None
        inv = or_h if or_h - or_l <= atr5 else (or_h + or_l) / 2
        return self._emit(t, st, "ORB_BREAKDOWN", now, or_l, inv, or_high=or_h, or_low=or_l)

    def _gap_down(self, t: str, st: SymbolState, bd: BearDay, bar: Bar, now: datetime
                  ) -> BearSignal | None:
        if bd.gap_done or _t(bar.end) > time(10, 0) or st.or5 is None:
            return None
        lv = self.ta.levels.get(t)
        if lv is None or st.open_price is None or not lv.prior_close:
            return None
        g = (st.open_price / lv.prior_close - 1) * 100
        if g > -2.0:
            return None
        news = self.ta.news.get(t)
        if news is None or news.catalyst.lower() in ("", "none"):
            return None
        level = min(st.pm_low, st.or5[1]) if st.pm_low is not None and st.pm_bars else st.or5[1]
        if bar.c >= level:
            return None
        bd.gap_done = True
        return self._emit(t, st, "GAP_DOWN_AND_GO", now, level, st.or5[0], gap_pct=round(g, 2),
                          catalyst=news.catalyst)

    def _failed_breakout(self, t: str, st: SymbolState, bd: BearDay, bar: Bar, now: datetime
                         ) -> BearSignal | None:
        if bd.failed_bo_done or st.or5 is None or st.hod is None:
            return None
        or_h = st.or5[0]
        if bd.breakout_at is None:
            if bar.h > or_h and bar.c > or_h:
                bd.breakout_at = bar.end
            return None
        if bar.end - bd.breakout_at > timedelta(minutes=15):
            bd.breakout_at = None if bar.c <= or_h else bd.breakout_at
            return None
        if bar.c < or_h:
            bd.failed_bo_done = True
            window = "morning" if _t(bar.end) <= time(10, 15) else "afternoon"
            return self._emit(t, st, "FAILED_BREAKOUT", now, bar.c, st.hod, window=window,
                              or_high=or_h, hod=st.hod)
        return None

    def _vwap_rejection(self, t: str, st: SymbolState, bd: BearDay, bar: Bar, now: datetime,
                        end: time) -> BearSignal | None:
        vw = st.vwap_value
        if bd.rejection_done or vw is None:
            return None
        if bar.h >= vw > bar.c:                       # failed reclaim
            bd.failed_reclaim_high = max(bd.failed_reclaim_high or 0.0, bar.h)
            return None
        prev = st.bars5[-2] if len(st.bars5) >= 2 else None
        if (bd.failed_reclaim_high is not None and prev is not None and bar.h < vw
                and bar.h < bd.failed_reclaim_high and bar.c < prev.l):
            bd.rejection_done = True
            window = "morning" if end <= time(10, 15) else "afternoon"
            return self._emit(t, st, "VWAP_REJECTION", now, bar.c, bd.failed_reclaim_high,
                              window=window, vwap=vw)
        return None

    def _close_near_low(self, t: str, st: SymbolState, bd: BearDay, bar: Bar, now: datetime
                        ) -> BearSignal | None:
        if bd.swing_done or st.hod is None or st.lod is None or st.hod <= st.lod:
            return None
        lv = self.ta.levels.get(t)
        vw = st.vwap_value
        pos = (bar.c - st.lod) / (st.hod - st.lod)
        if not (pos <= 0.10 and vw is not None and bar.c < vw and lv is not None
                and lv.ema10d is not None and bar.c < lv.ema10d and lv.atr14d):
            return None
        bd.swing_done = True
        inv = min(st.hod, bar.c + lv.atr14d)
        return self._emit(t, st, "CLOSE_NEAR_LOW", now, bar.c, inv, window="afternoon",
                          style="swing", t1_r=1.5, t2_r=3.0, range_pos=round(pos, 3))
