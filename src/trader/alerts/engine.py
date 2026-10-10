"""Pure alert engine: trades and 1-minute bars in, ``Alert`` objects out.

No I/O and no clock reads (time comes from the data), so it is cheap, deterministic and unit
tested with plain numbers. Two kinds of alerts:

- **Price rules** set by the owner (``above``/``below`` a price, ``up_pct``/``down_pct`` on the
  day) are checked on every trade, so they fire within about a second of the print.
- **Scanner signals** on every watched symbol are checked when a 1-minute bar closes (less noise
  than single prints): gap at the open, opening-range breakout/breakdown, VWAP reclaim/loss,
  new high/low of day, volume spike and ±N% moves from the prior close.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trader.alerts.config import SignalSettings
from trader.alerts.store import Rule

REARM_PCT = 0.15     # a repeating price rule re-arms after price moves back this far (%)


@dataclass(frozen=True)
class Alert:
    symbol: str
    kind: str
    text: str
    price: float | None
    ts: datetime
    rule_id: int | None = None
    consumed: bool = False      # one-shot rule fired: delete it


@dataclass(frozen=True)
class BarIn:
    symbol: str
    ts: datetime                # bar start
    o: float
    h: float
    l: float   # noqa: E741
    c: float
    v: float


@dataclass
class _RuleState:
    rule: Rule
    armed: bool = True


@dataclass
class SymbolState:
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    last: float | None = None
    last_ts: datetime | None = None
    pv: float = 0.0
    cum_vol: float = 0.0
    or_high: float | None = None
    or_low: float | None = None
    or_done: bool = False
    above_vwap: bool | None = None
    move_level: int = 0
    vols: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    suspect: float | None = None
    fired: set[str] = field(default_factory=set)
    cooldown_until: dict[str, datetime] = field(default_factory=dict)

    @property
    def vwap(self) -> float | None:
        return self.pv / self.cum_vol if self.cum_vol > 0 else None

    def change_pct(self, price: float | None = None) -> float | None:
        p = self.last if price is None else price
        if p is None or not self.prev_close:
            return None
        return (p / self.prev_close - 1) * 100


def _fmt_pct(x: float | None) -> str:
    return "" if x is None else f"{x:+.2f}%"


def _px(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:,.2f}" if x >= 1 else f"{x:.4f}"


class AlertEngine:
    def __init__(self, signals: SignalSettings, bad_print_pct: float = 5.0) -> None:
        self.cfg = signals
        self.bad_print_pct = bad_print_pct
        self.state: dict[str, SymbolState] = {}
        self._rules: dict[str, list[_RuleState]] = {}
        self.session_open: datetime | None = None

    # --- session / rules ---------------------------------------------------------------
    def start_session(self, session_open: datetime,
                      prev_closes: dict[str, float | None]) -> None:
        """New trading day: reset intraday state; keep rules (and their armed flags)."""
        self.session_open = session_open
        self.state = {sym: SymbolState(prev_close=pc) for sym, pc in prev_closes.items()}

    def ensure_symbol(self, symbol: str, prev_close: float | None = None,
                      last: float | None = None) -> SymbolState:
        st = self.state.get(symbol)
        if st is None:
            st = self.state[symbol] = SymbolState(prev_close=prev_close, last=last)
        elif prev_close is not None and st.prev_close is None:
            st.prev_close = prev_close
        return st

    def drop_symbol(self, symbol: str) -> None:
        self.state.pop(symbol, None)

    def set_rules(self, rules: list[Rule]) -> None:
        """Replace the rule set, keeping the armed state of rules that still exist."""
        old = {rs.rule.id: rs for lst in self._rules.values() for rs in lst}
        new: dict[str, list[_RuleState]] = {}
        for r in rules:
            prev = old.get(r.id)
            new.setdefault(r.symbol, []).append(
                _RuleState(r, prev.armed if prev is not None else True))
        self._rules = new

    def rule_symbols(self) -> set[str]:
        return {s for s, lst in self._rules.items() if lst}

    # --- trades: price rules -----------------------------------------------------------
    def on_trade(self, symbol: str, price: float, ts: datetime) -> list[Alert]:
        st = self.ensure_symbol(symbol)
        if price <= 0:
            return []
        if st.last and abs(price / st.last - 1) * 100 > self.bad_print_pct:
            # a lone far-away print is held until a second print confirms it (bad ticks)
            if st.suspect is None or abs(price / st.suspect - 1) * 100 > 1.0:
                st.suspect = price
                return []
        st.suspect = None
        st.last, st.last_ts = price, ts
        return self._check_rules(symbol, st, price, ts)

    def _check_rules(self, symbol: str, st: SymbolState, price: float, ts: datetime
                     ) -> list[Alert]:
        lst = self._rules.get(symbol)
        if not lst:
            return []
        out: list[Alert] = []
        chg = st.change_pct(price)
        keep: list[_RuleState] = []
        for rs in lst:
            r = rs.rule
            hit, rearm = self._rule_hit(r, price, chg)
            if rs.armed and hit:
                rs.armed = False
                out.append(self._rule_alert(symbol, st, r, price, ts, chg))
                if not r.repeat:
                    continue      # one-shot: drop it
            elif not rs.armed and rearm:
                rs.armed = True
            keep.append(rs)
        self._rules[symbol] = keep
        return out

    @staticmethod
    def _rule_hit(r: Rule, price: float, chg: float | None) -> tuple[bool, bool]:
        """(condition met, far enough back to re-arm a repeating rule)."""
        band = REARM_PCT / 100
        if r.kind == "above":
            return price >= r.value, price < r.value * (1 - band)
        if r.kind == "below":
            return price <= r.value, price > r.value * (1 + band)
        if chg is None:
            return False, False
        if r.kind == "up_pct":
            return chg >= r.value, chg < r.value - REARM_PCT
        return chg <= -r.value, chg > -r.value + REARM_PCT   # down_pct

    @staticmethod
    def _rule_alert(symbol: str, st: SymbolState, r: Rule, price: float, ts: datetime,
                    chg: float | None) -> Alert:
        icon = "🔔" if r.kind in ("above", "up_pct") else "🔻"
        cond = {"above": f"crossed above {_px(r.value)}",
                "below": f"crossed below {_px(r.value)}",
                "up_pct": f"up {r.value:g}% on the day",
                "down_pct": f"down {r.value:g}% on the day"}[r.kind]
        note = f"\n📝 {r.note}" if r.note else ""
        tail = "" if r.repeat else " · rule removed"
        text = (f"{icon} {symbol} {cond}: {_px(price)} {_fmt_pct(chg)}"
                f"{_vwap_note(st, price)}\nrule #{r.id}{tail}{note}")
        return Alert(symbol, f"rule_{r.kind}", text, price, ts, rule_id=r.id,
                     consumed=not r.repeat)

    # --- bars: scanner signals ---------------------------------------------------------
    def minute_of(self, ts: datetime) -> int | None:
        if self.session_open is None:
            return None
        return int((ts - self.session_open).total_seconds() // 60)

    def on_bar(self, bar: BarIn, quiet: bool = False) -> list[Alert]:
        """Process a closed 1-minute bar. ``quiet`` updates state without alerting (warm-up
        after a mid-session start)."""
        st = self.ensure_symbol(bar.symbol)
        minute = self.minute_of(bar.ts)
        end = bar.ts + timedelta(minutes=1)
        if st.last_ts is None or end >= st.last_ts:
            st.last, st.last_ts = bar.c, end
        if minute is None or minute < 0 or minute >= 390:
            return []   # extended hours: price only
        cfg, sym, out = self.cfg, bar.symbol, []

        def emit(kind: str, text: str, cooldown: bool = False, once: bool = False) -> None:
            if once:
                if kind in st.fired:
                    return
                st.fired.add(kind)
            if cooldown:
                until = st.cooldown_until.get(kind)
                if until is not None and end < until:
                    return
                st.cooldown_until[kind] = end + timedelta(minutes=cfg.cooldown_min)
            if not quiet:
                out.append(Alert(sym, kind, text, bar.c, end))

        first = st.open is None
        if first:
            st.open = bar.o
            if cfg.gap and st.prev_close:
                gap = (bar.o / st.prev_close - 1) * 100
                if abs(gap) >= cfg.gap_pct:
                    icon = "⬆️" if gap > 0 else "⬇️"
                    emit("gap", f"{icon} {sym} gapped {gap:+.2f}%: open {_px(bar.o)} vs prior "
                         f"close {_px(st.prev_close)}", once=True)
        prior_high, prior_low = st.high, st.low
        st.high = bar.h if st.high is None else max(st.high, bar.h)
        st.low = bar.l if st.low is None else min(st.low, bar.l)
        st.pv += (bar.h + bar.l + bar.c) / 3 * bar.v
        st.cum_vol += bar.v
        vwap = st.vwap
        chg = st.change_pct(bar.c)
        ctx = f" {_fmt_pct(chg)}" if chg is not None else ""

        # opening range
        if minute < cfg.opening_range_min:
            st.or_high = bar.h if st.or_high is None else max(st.or_high, bar.h)
            st.or_low = bar.l if st.or_low is None else min(st.or_low, bar.l)
        elif not st.or_done:
            st.or_done = st.or_high is not None
        n_before = len(out)
        if cfg.orb and st.or_done and st.or_high is not None and st.or_low is not None:
            if bar.c > st.or_high:
                emit("orb_up", f"🚀 {sym} opening-range breakout: {_px(bar.c)} > OR high "
                     f"{_px(st.or_high)} ({cfg.opening_range_min}m){ctx}"
                     f"{_vwap_note(st, bar.c)}", once=True)
            elif bar.c < st.or_low:
                emit("orb_down", f"📉 {sym} opening-range breakdown: {_px(bar.c)} < OR low "
                     f"{_px(st.or_low)} ({cfg.opening_range_min}m){ctx}"
                     f"{_vwap_note(st, bar.c)}", once=True)

        # new high / low of day (after the opening range, so the first minutes stay quiet;
        # skipped on the bar an ORB alert already covers)
        orb_now = len(out) > n_before
        if (cfg.new_high_low and st.or_done and not orb_now and prior_high is not None
                and prior_low is not None):
            if bar.h > prior_high:
                emit("hod", f"⬆️ {sym} new high of day {_px(bar.h)}{ctx}", cooldown=True)
            elif bar.l < prior_low:
                emit("lod", f"⬇️ {sym} new low of day {_px(bar.l)}{ctx}", cooldown=True)

        # VWAP reclaim / loss (from minute 5, once VWAP has some volume behind it)
        if vwap is not None:
            side = bar.c > vwap
            if (cfg.vwap_cross and st.above_vwap is not None and side != st.above_vwap
                    and minute >= 5):
                if side:
                    emit("vwap_up", f"↗️ {sym} reclaimed VWAP: {_px(bar.c)} > {_px(vwap)}{ctx}",
                         cooldown=True)
                else:
                    emit("vwap_down", f"↘️ {sym} lost VWAP: {_px(bar.c)} < {_px(vwap)}{ctx}",
                         cooldown=True)
            st.above_vwap = side

        # volume spike vs the last 20 one-minute bars
        if cfg.volume_spike and len(st.vols) >= 10:
            avg = sum(st.vols) / len(st.vols)
            if avg >= cfg.volume_spike_min_avg and bar.v >= cfg.volume_spike_x * avg:
                arrow = "🟢" if bar.c >= bar.o else "🔴"
                emit("volume", f"{arrow} {sym} volume spike {bar.v / avg:.1f}x "
                     f"({int(bar.v):,} vs avg {int(avg):,}) at {_px(bar.c)}{ctx}",
                     cooldown=True)
        st.vols.append(bar.v)

        # every ±step% from the prior close, each level once per day
        if cfg.move_step_pct > 0 and chg is not None:
            level = int(chg / cfg.move_step_pct)
            if level != 0 and (level * st.move_level < 0 or abs(level) > abs(st.move_level)):
                st.move_level = level
                icon = "📈" if level > 0 else "📉"
                emit(f"move_{level}", f"{icon} {sym} {_fmt_pct(chg)} on the day at "
                     f"{_px(bar.c)} (prior close {_px(st.prev_close)})"
                     f"{_vwap_note(st, bar.c)}")
        return out

    # --- read-outs ---------------------------------------------------------------------
    def quote_line(self, symbol: str) -> str:
        st = self.state.get(symbol)
        if st is None or st.last is None:
            return f"{symbol}: no data yet"
        parts = [f"{symbol} {_px(st.last)} {_fmt_pct(st.change_pct())}".rstrip()]
        if st.vwap is not None:
            parts.append(f"VWAP {_px(st.vwap)}")
        if st.high is not None and st.low is not None:
            parts.append(f"range {_px(st.low)}–{_px(st.high)}")
        if st.or_done and st.or_high is not None:
            parts.append(f"OR {_px(st.or_low)}–{_px(st.or_high)}")
        return " · ".join(parts)

    def movers(self) -> list[tuple[str, float]]:
        out = [(s, c) for s, st in self.state.items()
               if (c := st.change_pct()) is not None]
        return sorted(out, key=lambda x: x[1], reverse=True)


def _vwap_note(st: SymbolState, price: float) -> str:
    v = st.vwap
    if v is None:
        return ""
    return f" · {'above' if price >= v else 'below'} VWAP {_px(v)}"
