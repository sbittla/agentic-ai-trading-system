"""Owner alerts produced by trader-core (REQUIREMENTS §10.5, AL-1..AL-7).

trader-core already streams every watched symbol (one Alpaca WebSocket, AL-1), so it detects
the same scanner signals as the standalone bot (``trader.alerts.engine``) and adds what makes
an alert useful to a day trader:

- AL-2 a label: **qualified** (would pass the system's own checks now) or **raw**, with the
  checklist score and the first failed item;
- AL-3 a plan from the same A4/A7 code as trades: trigger, invalidation, T1/T2, share size,
  R:R to the next resistance, RVOL, VWAP/QQQ position;
- AL-4 a schedule (pre-market and the two windows by default), AL-5 caps, AL-6 ranking with a
  10-minute digest for the rest, AL-7 grading (+1R before -1R within 30 minutes).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

import structlog

from trader.agents.a02_market_data import MarketDataAgent
from trader.agents.a04_technicals import TechnicalAgent
from trader.agents.a07_risk import RiskManager
from trader.alerts.config import SignalSettings
from trader.alerts.engine import Alert, AlertEngine, BarIn
from trader.calendar import TradingWindows
from trader.config import Config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import Bar
from trader.timeutil import cents, cents_down, et_datetime, to_et

log = structlog.get_logger(__name__)
UP_KINDS = {"orb_up", "hod", "vwap_up"}
DOWN_KINDS = {"orb_down", "lod", "vwap_down"}


@dataclass
class Plan:
    direction: str
    trigger: Decimal
    stop: Decimal
    t1: Decimal
    t2: Decimal
    shares: int
    risk_usd: Decimal
    rr_resistance: float | None
    rvol: float | None
    above_vwap: bool | None
    qqq_above_vwap: bool | None

    def text(self) -> str:
        side = "long" if self.direction == "long" else "short view (options only, BR-4)"
        size = (f"{self.shares} sh (${self.risk_usd:,.0f} risk)" if self.direction == "long"
                else "no stock order")
        rr = f"{self.rr_resistance:.1f}" if self.rr_resistance is not None else "open"
        rvol = f"{self.rvol:.1f}" if self.rvol is not None else "?"
        vw = {True: "above", False: "below", None: "?"}
        return (f"Plan: {side} trigger {self.trigger} · stop {self.stop} · T1 {self.t1} · "
                f"T2 {self.t2} · {size} · R:R to resistance {rr} · RVOL {rvol} · "
                f"{vw[self.above_vwap]} VWAP, QQQ {vw[self.qqq_above_vwap]} VWAP")


@dataclass
class Logged:
    row_id: int | None
    ts: datetime
    ticker: str
    kind: str
    direction: str
    stop: Decimal
    t1: Decimal
    graded: bool = False


@dataclass
class CoreAlerts:
    cfg: Config
    md: MarketDataAgent
    ta: TechnicalAgent
    risk: RiskManager
    windows: TradingWindows
    journal: SqlJournal | None
    locked: Any = None                     # callable(now) -> set of locked tickers
    equity: Any = None                     # callable() -> Decimal
    engine: AlertEngine = field(init=False)
    muted: bool = False                    # ALERTS OFF command
    sent: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    window_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    bucket: int | None = None
    bucket_sent: int = 0
    digest: list[tuple[float, str]] = field(default_factory=list)
    pending: list[Logged] = field(default_factory=list)
    signals: list[tuple[str, str, Any, str, int]] = field(default_factory=list)  # OP-10 (2)

    def __post_init__(self) -> None:
        self.engine = AlertEngine(SignalSettings())

    def start_day(self, session_open: datetime, prev_closes: dict[str, float | None]) -> None:
        self.engine.start_session(session_open, prev_closes)
        self.sent.clear()
        self.window_total.clear()
        self.bucket, self.bucket_sent, self.digest = None, 0, []

    # -- AL-4 schedule --------------------------------------------------------------------
    def window_of(self, now: datetime) -> str | None:
        t = to_et(now).time()
        if self.cfg.alerts.schedule == "all_day":
            return "day" if time(4, 0) <= t < time(20, 0) else None
        if time(7, 0) <= t < time(9, 30):
            return "pre"
        if time(9, 30) <= t < time(10, 30):
            return "morning"
        if time(15, 0) <= t < time(16, 0):
            return "afternoon"
        return None

    # -- AL-2 label -----------------------------------------------------------------------
    def checklist(self, ticker: str, direction: str, now: datetime) -> tuple[int, str | None]:
        """Ten checks the system would run now; returns (passed, first failed)."""
        checks: list[tuple[str, bool]] = []
        locked = set(self.locked(now)) if callable(self.locked) else set()
        g = self.cfg.canonical(ticker)
        checks.append(("CK-1 on list, not locked", g in self.cfg.universe.tickers
                       and g not in locked))
        checks.append(("CK-2 entry window", self.windows.entry_window(now, "day")[0]
                       is not None))
        st = self.md.state.get(ticker)
        if st is None:
            return sum(ok for _, ok in checks), next((n for n, ok in checks if not ok),
                                                     "not streamed")
        try:
            f = self.ta.filters(ticker, now, "ORB", 80)
        except Exception:   # noqa: BLE001
            f = {}
        if direction == "short":   # BR-2: the bearish mirror of MF-1
            qqq = self.md.state.get("QQQ")
            rs = self.ta.rel_strength(st)
            f["MF-1"] = bool((qqq and qqq.above_vwap() is False) or (rs is not None and rs < 0))
        for k in ("MF-1", "MF-2", "MF-3", "MF-4", "MF-5", "MF-6"):
            checks.append((k, bool(f.get(k, False))))
        av = st.above_vwap()
        checks.append(("VWAP side", av is True if direction == "long" else av is False))
        checks.append(("no news shock", g not in self.risk.state.news_blocked))
        passed = sum(ok for _, ok in checks)
        return passed, next((n for n, ok in checks if not ok), None)

    # -- AL-3 plan ------------------------------------------------------------------------
    def plan(self, ticker: str, direction: str, price: float) -> Plan | None:
        st = self.md.state.get(ticker)
        if st is None or not st.atr5.value:
            return None
        atr5 = st.atr5.value
        levels = [x for x in (st.vwap_value, st.or5[1] if st.or5 else None,
                              st.or5[0] if st.or5 else None,
                              *(b.l for b in st.bars1[-3:]), *(b.h for b in st.bars1[-3:]))
                  if x is not None]
        if direction == "long":
            below = [x for x in levels if price - atr5 <= x < price - 0.1 * atr5]
            stop = cents_down((max(below) if below else price - atr5) - 0.05 * atr5)
            r = Decimal(str(price)) - stop
            trig = cents(price)
            t1, t2 = cents(trig + r), cents(trig + 2 * r)
            res = self.ta.resistance(ticker, price, st)
            rr = (res - price) / float(r) if res is not None and r > 0 else None
            eq = self.equity() if callable(self.equity) else Decimal("0")
            qty, risk_usd, _, _ = self.risk.size(trig, stop, eq, eq, Decimal(0), False)
        else:
            above = [x for x in levels if price + 0.1 * atr5 < x <= price + atr5]
            stop = cents((min(above) if above else price + atr5) + 0.05 * atr5)
            r = stop - Decimal(str(price))
            trig = cents(price)
            t1, t2 = cents(trig - r), cents(trig - 2 * r)
            lows = [x for x in (st.lod, self.ta.levels[ticker].prior_low
                                if ticker in self.ta.levels else None) if x is not None]
            sup = max((x for x in lows if x < price), default=None)
            rr = (price - sup) / float(r) if sup is not None and r > 0 else None
            qty, risk_usd = 0, Decimal(0)
        if r <= 0:
            return None
        qqq = self.md.state.get("QQQ")
        return Plan(direction, trig, stop, t1, t2, qty, risk_usd, rr, st.rvol(),
                    st.above_vwap(), qqq.above_vwap() if qqq else None)

    def quality(self, plan: Plan | None, passed: int, signal_alignment: bool) -> float:
        """AL-6: 0-100 from RVOL, alignment with QQQ, room to resistance and the checklist."""
        if plan is None:
            return passed * 3.0
        rv = min((plan.rvol or 0) / 3.0, 1.0) * 30
        al = 20 if signal_alignment else 0
        room = min((plan.rr_resistance if plan.rr_resistance is not None else 3.0) / 3.0,
                   1.0) * 20
        return round(rv + al + room + passed * 3.0, 1)

    # -- stream -----------------------------------------------------------------------------
    def direction(self, a: Alert, bar: Bar) -> str | None:
        if a.kind in UP_KINDS:
            return "long"
        if a.kind in DOWN_KINDS:
            return "short"
        if a.kind == "volume":
            return "long" if bar.c >= bar.o else "short"
        if a.kind.startswith("move_"):
            return "long" if not a.kind.startswith("move_-") else "short"
        if a.kind == "gap":
            st = self.md.state.get(a.symbol)
            pc = st.prior_close if st else None
            return "long" if pc is None or bar.o >= pc else "short"
        return None

    def on_bar(self, bar: Bar, now: datetime) -> list[str]:
        """Feed a closed 1-min bar; returns the alert texts to send now."""
        if not self.cfg.alerts.enabled:
            return []
        raw = self.engine.on_bar(BarIn(bar.symbol, bar.ts, bar.o, bar.h, bar.l, bar.c,
                                       float(bar.v)))
        out: list[str] = []
        for a in raw:
            text = self._handle(a, bar, now)
            if text:
                out.append(text)
        return out

    def _handle(self, a: Alert, bar: Bar, now: datetime) -> str | None:
        direction = self.direction(a, bar)
        if direction is None:
            return None
        passed, failed = self.checklist(a.symbol, direction, now)
        label = "qualified" if passed == 10 else "raw"
        plan = self.plan(a.symbol, direction, a.price or bar.c)
        qqq = self.md.state.get("QQQ")
        aligned = bool(qqq and qqq.above_vwap() is (direction == "long"))
        q = self.quality(plan, passed, aligned)
        head = (f"[{label} {passed}/10" + (f": failed {failed}" if failed else "") + "] ")
        text = head + a.text + ("\n" + plan.text() if plan else "")
        w = self.window_of(now)
        delivery = self._delivery(a, w, now)
        if delivery == "sent" and self.bucket_sent >= self.cfg.alerts.top_n:
            delivery = "digest"
        if delivery == "sent":
            self.bucket_sent += 1
        elif delivery == "digest":
            self.digest.append((q, text))
        row_id = self._log(now, a, direction, label, passed, failed, q, delivery, plan, text)
        if plan is not None and direction in ("long", "short"):
            self.signals.append((a.symbol, direction, plan, a.kind, passed))
            self.pending.append(Logged(row_id, now, a.symbol, a.kind, direction, plan.stop,
                                       plan.t1))
        return text if delivery == "sent" else None

    def _delivery(self, a: Alert, w: str | None, now: datetime) -> str:
        if self.muted:
            return "muted"
        if w is None or a.kind in self.cfg.alerts.disabled_kinds:
            return "off"
        bucket = int(to_et(now).timestamp() // (self.cfg.alerts.digest_min * 60))
        if bucket != self.bucket:
            self.bucket, self.bucket_sent = bucket, 0
        if (self.sent[(w, a.symbol)] >= self.cfg.alerts.per_symbol_cap
                or self.window_total[w] >= self.cfg.alerts.per_window_cap):
            return "capped"
        self.sent[(w, a.symbol)] += 1
        self.window_total[w] += 1
        return "sent"

    def flush(self, now: datetime) -> str | None:
        """AL-6: at the end of each 10-minute bucket, one digest message for the rest."""
        bucket = int(to_et(now).timestamp() // (self.cfg.alerts.digest_min * 60))
        if not self.digest or bucket == self.bucket:
            return None
        items = sorted(self.digest, key=lambda x: -x[0])
        self.digest = []
        self.bucket, self.bucket_sent = bucket, 0
        lines = [f"Alert digest ({len(items)}, best first):"]
        lines += [f"· {t.splitlines()[0]}" for _, t in items[:15]]
        return "\n".join(lines)

    # -- AL-7 grading -----------------------------------------------------------------------
    def grade(self, now: datetime) -> int:
        """Grade alerts older than the window: did price reach +1R before -1R?"""
        win = timedelta(minutes=self.cfg.alerts.grade_window_min)
        done = 0
        for lg in list(self.pending):
            if now - lg.ts < win:
                continue
            st = self.md.state.get(lg.ticker)
            hit = False
            if st is not None:
                for b in st.bars1:
                    if b.ts < lg.ts or b.ts > lg.ts + win:
                        continue
                    lo, hi = Decimal(str(b.l)), Decimal(str(b.h))
                    if lg.direction == "long":
                        if lo <= lg.stop:
                            break
                        if hi >= lg.t1:
                            hit = True
                            break
                    else:
                        if hi >= lg.stop:
                            break
                        if lo <= lg.t1:
                            hit = True
                            break
            self.pending.remove(lg)
            done += 1
            if self.journal is not None and lg.row_id is not None:
                try:
                    self.journal.update(s.alerts_log, {"id": lg.row_id},
                                        {"hit": hit, "graded_at": now})
                except Exception as exc:   # noqa: BLE001
                    log.warning("alert_grade_failed", error=str(exc))
        return done

    def _log(self, now: datetime, a: Alert, direction: str, label: str, passed: int,
             failed: str | None, q: float, delivery: str, plan: Plan | None, text: str
             ) -> int | None:
        if self.journal is None:
            return None
        try:
            return self.journal.insert(s.alerts_log, {
                "ts": now, "ticker": a.symbol, "kind": a.kind, "direction": direction,
                "label": label, "checklist_score": passed, "first_failed": failed,
                "quality": q, "delivery": delivery, "price": a.price,
                "stop": plan.stop if plan else None, "t1": plan.t1 if plan else None,
                "text": text, "plan_json": {
                    "trigger": str(plan.trigger), "stop": str(plan.stop), "t1": str(plan.t1),
                    "t2": str(plan.t2), "shares": plan.shares,
                    "rr_resistance": plan.rr_resistance} if plan else None})
        except Exception as exc:   # noqa: BLE001 - alerts never block trading
            log.warning("alert_log_failed", error=str(exc))
            return None


def grading_report(rows: list[dict[str, Any]], min_graded: int, min_hit_rate: float
                   ) -> tuple[str, list[str]]:
    """AL-7: hit rate by alert type; types below ``min_hit_rate`` over ``min_graded``+ graded
    alerts are flagged (the owner can turn them off with ``alerts.disabled_kinds``)."""
    by: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        if r.get("hit") is not None:
            by[str(r["kind"])].append(bool(r["hit"]))
    if not by:
        return "Alerts (AL-7): none graded yet", []
    lines = ["Alerts (AL-7): +1R before -1R within 30 min, by type"]
    flags = []
    for kind, v in sorted(by.items(), key=lambda x: -len(x[1])):
        rate = sum(v) / len(v)
        lines.append(f"  {kind}: {rate:.0%} of {len(v)}")
        if len(v) >= min_graded and rate < min_hit_rate:
            flags.append(f"{kind}: {rate:.0%} over {len(v)} alerts (< {min_hit_rate:.0%}); "
                         "consider alerts.disabled_kinds")
    return "\n".join(lines), flags


def session_open_for(d: Any) -> datetime:
    return et_datetime(d, time(9, 30))
