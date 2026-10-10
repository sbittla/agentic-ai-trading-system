"""A2 Market data agent (A2-1..A2-5, DQ-1..DQ-5).

Turns the 1-min IEX bar stream into per-symbol session state: VWAP, opening ranges,
high/low of day, 5-min bars, time-of-day RVOL, ATR(5-min) and intraday EMAs (carried across
days). Flags stale data and gaps; bad bars are held until confirmed (DQ-3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import structlog

from trader.indicators import Ema, RvolProfile, SessionVwap, WilderAtr
from trader.models.messages import Bar, Quote, TradingStatus
from trader.timeutil import ET, et_datetime, minutes_since_open, to_et

log = structlog.get_logger(__name__)

OPEN = time(9, 30)
PREMARKET_FROM = time(4, 0)


@dataclass
class SymbolState:
    symbol: str
    day: date | None = None
    close_time: time = time(16, 0)
    # session (reset at 9:30)
    vwap: SessionVwap = field(default_factory=SessionVwap)
    bars1: list[Bar] = field(default_factory=list)
    bars5: list[Bar] = field(default_factory=list)
    vwap_at_5m: list[float] = field(default_factory=list)
    ema9_at_5m: list[float | None] = field(default_factory=list)
    open_price: float | None = None
    hod: float | None = None
    lod: float | None = None
    hod_time: datetime | None = None
    rvol_at_hod: float | None = None
    hod_before_1500: float | None = None
    cum_vol: float = 0.0
    or5: tuple[float, float] | None = None
    or15: tuple[float, float] | None = None
    or5_avg_vol: float | None = None
    or15_avg_vol: float | None = None
    pm_high: float | None = None
    pm_low: float | None = None
    pm_bars: int = 0
    last_price: float | None = None
    last_bar_end: datetime | None = None
    minute_volumes: list[tuple[int, float]] = field(default_factory=list)
    incomplete: bool = False
    # carried across days
    atr5: WilderAtr = field(default_factory=lambda: WilderAtr(14))
    ema9_5m: Ema = field(default_factory=lambda: Ema(9))
    ema20_5m: Ema = field(default_factory=lambda: Ema(20))
    rvol_profile: RvolProfile = field(default_factory=RvolProfile)
    # aggregation and quality
    pending5: list[Bar] = field(default_factory=list)
    suspect: Bar | None = None
    quote: Quote | None = None
    halted: bool = False
    last_halt_event: datetime | None = None
    resumed_at: datetime | None = None
    prior_close: float | None = None

    # -- derived -------------------------------------------------------------------------
    @property
    def vwap_value(self) -> float | None:
        return self.vwap.value

    def minute_index(self, now: datetime) -> int:
        return minutes_since_open(now)

    def rvol(self) -> float | None:
        if self.last_bar_end is None:
            return None
        minute = max(minutes_since_open(self.last_bar_end) - 1, 0)
        return self.rvol_profile.rvol(minute, self.cum_vol)

    def day_change_pct(self) -> float | None:
        if self.last_price is None or not self.prior_close:
            return None
        return (self.last_price - self.prior_close) / self.prior_close * 100

    def change_from_open_pct(self) -> float | None:
        if self.last_price is None or not self.open_price:
            return None
        return (self.last_price - self.open_price) / self.open_price * 100

    def above_vwap(self) -> bool | None:
        v = self.vwap_value
        return None if v is None or self.last_price is None else self.last_price > v

    def trend_day(self) -> bool:
        """At least 70% of closed 5-min bars since 9:30 closed above VWAP."""
        if not self.bars5:
            return False
        above = sum(1 for b, v in zip(self.bars5, self.vwap_at_5m, strict=True) if b.c > v)
        return above / len(self.bars5) >= 0.70


class MarketDataAgent:
    """A2: one instance for all symbols (single Alpaca stream, ARCHITECTURE §1)."""

    def __init__(self, symbols: list[str], stale_after_sec: int = 60) -> None:
        self.symbols = symbols
        self.stale_after = timedelta(seconds=stale_after_sec)
        self.state: dict[str, SymbolState] = {s: SymbolState(s) for s in symbols}
        self.last_message_at: datetime | None = None

    # -- day lifecycle ---------------------------------------------------------------------
    def start_day(self, d: date, close_time: time = time(16, 0),
                  prior_closes: dict[str, float] | None = None) -> None:
        """Reset session indicators at the start of a trading day; carry ATR5/EMAs/RVOL."""
        for sym, st in self.state.items():
            if st.day is not None and st.day != d and st.minute_volumes:
                st.rvol_profile.add_session(st.minute_volumes)
            if st.day is not None and st.day != d and st.last_price is not None:
                st.prior_close = st.last_price
            if prior_closes and sym in prior_closes:
                st.prior_close = prior_closes[sym]
            st.day = d
            st.close_time = close_time
            st.vwap = SessionVwap()
            st.bars1, st.bars5, st.vwap_at_5m, st.ema9_at_5m = [], [], [], []
            st.open_price = st.hod = st.lod = st.hod_before_1500 = None
            st.hod_time = None
            st.rvol_at_hod = None
            st.cum_vol = 0.0
            st.or5 = st.or15 = None
            st.or5_avg_vol = st.or15_avg_vol = None
            st.pm_high = st.pm_low = None
            st.pm_bars = 0
            st.minute_volumes = []
            st.pending5 = []
            st.suspect = None
            st.incomplete = False
            st.halted = False

    def seed_history(self, symbol: str, minute_bars_by_day: list[list[Bar]]) -> None:
        """Warm RVOL profile, ATR5 and 5-min EMAs from history (oldest day first)."""
        st = self.state[symbol]
        for day_bars in minute_bars_by_day:
            vols: list[tuple[int, float]] = []
            buckets: dict[int, list[Bar]] = {}
            for b in day_bars:
                m = minutes_since_open(b.ts)
                if 0 <= m < 390:
                    vols.append((m, float(b.v)))
                    buckets.setdefault(m // 5, []).append(b)
            if vols:
                st.rvol_profile.add_session(vols)
            for k in sorted(buckets):
                bb = buckets[k]
                h, lo, c = max(x.h for x in bb), min(x.l for x in bb), bb[-1].c
                st.atr5.update(h, lo, c)
                st.ema9_5m.update(c)
                st.ema20_5m.update(c)
            if day_bars:
                st.last_price = day_bars[-1].c

    # -- stream ----------------------------------------------------------------------------
    def on_quote(self, q: Quote) -> None:
        st = self.state.get(q.symbol)
        self.last_message_at = max(self.last_message_at or q.ts, q.ts)
        if st is not None:
            st.quote = q

    def on_status(self, s: TradingStatus) -> None:
        st = self.state.get(s.symbol)
        if st is None:
            return
        if s.kind == "halt":
            st.halted = True
            st.last_halt_event = s.ts
        elif s.kind == "resume":
            st.halted = False
            st.resumed_at = s.ts
            st.last_halt_event = s.ts
        else:   # LULD band event
            st.last_halt_event = s.ts

    def on_bar(self, bar: Bar, *, received_at: datetime | None = None) -> list[Bar]:
        """Accept a closed 1-min bar. Returns 5-min bars that closed because of it."""
        st = self.state.get(bar.symbol)
        if st is None:
            return []
        self.last_message_at = max(self.last_message_at or bar.end, received_at or bar.end)
        local = to_et(bar.ts)
        if st.day is None or local.date() != st.day:
            self.start_day(local.date())
        t = local.time()
        if PREMARKET_FROM <= t < OPEN:
            st.pm_high = bar.h if st.pm_high is None else max(st.pm_high, bar.h)
            st.pm_low = bar.l if st.pm_low is None else min(st.pm_low, bar.l)
            st.pm_bars += 1
            return []
        if not (OPEN <= t < st.close_time):
            return []
        # DQ-3: zero volume or > 5% from the prior bar is held until the next bar confirms it
        if bar.v <= 0:
            log.info("dq3_zero_volume", symbol=bar.symbol, ts=bar.ts.isoformat())
            return []
        ref = st.last_price
        out: list[Bar] = []
        if st.suspect is not None:
            sus, st.suspect = st.suspect, None
            if abs(bar.c - sus.c) / sus.c <= 0.05:
                out += self._accept(st, sus)
            else:
                log.warning("dq3_dropped", symbol=sus.symbol, ts=sus.ts.isoformat())
                ref = st.last_price
        if ref is not None and abs(bar.c - ref) / ref > 0.05 and st.suspect is None and not out:
            st.suspect = bar
            log.warning("dq3_suspect", symbol=bar.symbol, ts=bar.ts.isoformat())
            return out
        # DQ-5: a gap in the stream marks indicators incomplete until back-filled
        if st.bars1 and (bar.ts - st.bars1[-1].ts) > timedelta(minutes=3):
            st.incomplete = True
        out += self._accept(st, bar)
        return out

    def _accept(self, st: SymbolState, bar: Bar) -> list[Bar]:
        if st.bars1 and bar.ts <= st.bars1[-1].ts:
            return []   # duplicate or out of order
        out: list[Bar] = []
        m = minutes_since_open(bar.ts)
        if st.pending5 and (minutes_since_open(st.pending5[0].ts) // 5) != m // 5:
            out.append(self._close5(st))
        st.bars1.append(bar)
        if st.open_price is None:
            st.open_price = bar.o
        st.vwap.update(bar.h, bar.l, bar.c, bar.v)
        st.cum_vol += bar.v
        st.minute_volumes.append((m, float(bar.v)))
        st.last_price = bar.c
        st.last_bar_end = bar.end
        if st.hod is None or bar.h > st.hod:
            st.hod = bar.h
            st.hod_time = bar.end
            st.rvol_at_hod = st.rvol()
        st.lod = bar.l if st.lod is None else min(st.lod, bar.l)
        if to_et(bar.end).time() <= time(15, 0):
            st.hod_before_1500 = st.hod
        if m < 5:
            st.or5 = (max(st.or5[0], bar.h), min(st.or5[1], bar.l)) if st.or5 else (bar.h, bar.l)
        if m < 15:
            st.or15 = ((max(st.or15[0], bar.h), min(st.or15[1], bar.l)) if st.or15
                       else (bar.h, bar.l))
        st.pending5.append(bar)
        if m % 5 == 4:
            out.append(self._close5(st))
        return out

    def _close5(self, st: SymbolState) -> Bar:
        bb, st.pending5 = st.pending5, []
        m0 = (minutes_since_open(bb[0].ts) // 5) * 5
        start = et_datetime(to_et(bb[0].ts).date(), OPEN) + timedelta(minutes=m0)
        b5 = Bar(symbol=st.symbol, ts=start, timeframe="5Min", o=bb[0].o,
                 h=max(b.h for b in bb), l=min(b.l for b in bb), c=bb[-1].c,
                 v=sum(b.v for b in bb), source=bb[0].source)
        st.bars5.append(b5)
        st.vwap_at_5m.append(st.vwap_value or b5.c)
        st.atr5.update(b5.h, b5.l, b5.c)
        st.ema9_5m.update(b5.c)
        st.ema20_5m.update(b5.c)
        st.ema9_at_5m.append(st.ema9_5m.value)
        if len(st.bars5) == 1:
            st.or5_avg_vol = float(b5.v)
        if len(st.bars5) == 3:
            st.or15_avg_vol = sum(b.v for b in st.bars5[:3]) / 3
        return b5

    def flush(self, now: datetime, grace_s: float = 0.0) -> list[Bar]:
        """Close 5-min buckets whose end time has passed with no last-minute bar."""
        out: list[Bar] = []
        for st in self.state.values():
            if not st.pending5:
                continue
            m0 = (minutes_since_open(st.pending5[0].ts) // 5) * 5
            end = et_datetime(to_et(st.pending5[0].ts).date(), OPEN) + timedelta(minutes=m0 + 5)
            if end + timedelta(seconds=grace_s) <= now:
                out.append(self._close5(st))
        return out

    # -- data health (A2-4) ----------------------------------------------------------------
    def is_stale(self, now: datetime) -> bool:
        return self.last_message_at is None or now - self.last_message_at > self.stale_after

    def symbol_stale(self, symbol: str, now: datetime) -> bool:
        st = self.state[symbol]
        return st.last_bar_end is None or now - st.last_bar_end > max(self.stale_after,
                                                                      timedelta(minutes=5))

    def recent_halt(self, symbol: str, now: datetime, lookback_min: int) -> bool:
        st = self.state[symbol]
        if st.halted:
            return True
        return st.last_halt_event is not None and now - st.last_halt_event < timedelta(
            minutes=lookback_min)

    def backfill(self, bars: list[Bar]) -> list[Bar]:
        """DQ-5: insert REST-fetched bars after a reconnect; clears the incomplete flag."""
        out: list[Bar] = []
        for b in sorted(bars, key=lambda x: x.ts):
            st = self.state.get(b.symbol)
            if st is None or (st.bars1 and b.ts <= st.bars1[-1].ts):
                continue
            out += self.on_bar(b)
        for b in bars:
            if b.symbol in self.state:
                self.state[b.symbol].incomplete = False
        return out

    def today_daily_bar(self, symbol: str) -> Bar | None:
        st = self.state[symbol]
        if st.open_price is None or st.hod is None or st.lod is None or st.last_price is None:
            return None
        if st.day is None:
            return None
        return Bar(symbol=symbol, ts=et_datetime(st.day, OPEN).astimezone(ET), timeframe="1Day",
                   o=st.open_price, h=st.hod, l=st.lod, c=st.last_price, v=int(st.cum_vol))
