"""TradingEngine: the decision-and-order path inside trader-core (ARCHITECTURE §1, §3, §4).

One engine runs live, paper and replay by swapping interfaces (clock, broker, LLM, notifier,
history). Every step writes to the journal before the next one acts (SR-4). Order of a trade:

    A4 SetupTriggered -> A7 precheck (code, CK-1..8) -> A5 proposal (Qwen) -> A6 review (Qwen)
    -> A12 compliance -> A7 checklist + size -> [owner approval] -> A8 brackets -> A9 exits
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import structlog

from trader.agents.a01_universe import UniverseAgent
from trader.agents.a02_market_data import MarketDataAgent
from trader.agents.a04_technicals import TechnicalAgent, compute_levels
from trader.agents.a05_strategy import StrategyAgent, proposal_id_for
from trader.agents.a06_review import ReviewAgent
from trader.agents.a07_risk import ClosedTrade, DecisionContext, OpenPositionView, RiskManager
from trader.agents.a08_execution import ExecutionAgent, marketable_exit_price
from trader.agents.a09_positions import (
    Action,
    ExitAction,
    ManagedPosition,
    PositionMonitor,
    StopMove,
    StopWidenError,
)
from trader.agents.a10_supervisor import Scheduler, Supervisor
from trader.agents.a11_reporter import Reporter
from trader.agents.a12_compliance import ComplianceGuard
from trader.bus import EventBus
from trader.calendar import MacroCalendar, MarketCalendar, TradingWindows
from trader.config import Config
from trader.interfaces import Broker, BrokerRejected, BrokerUnavailable, HistoricalData, LLMClient
from trader.journal import schema as s
from trader.journal.store import JournalUnavailable, SqlJournal
from trader.models.messages import (
    ApprovalResponse,
    Bar,
    ChecklistItem,
    Command,
    Decision,
    EventFlags,
    KillSwitch,
    LimitHit,
    NoTrade,
    OrderUpdate,
    PositionClosed,
    PositionOpened,
    Quote,
    SetupTriggered,
    StopMoved,
    TradeProposal,
    TradingStatus,
)
from trader.models.reasons import Reason
from trader.notify.commands import CommandAuthority
from trader.timeutil import ET, et_date, et_datetime, to_et

log = structlog.get_logger(__name__)
D0 = Decimal(0)


class ApprovalNotifier(Protocol):
    """What the engine needs from a notifier (INTERFACES.md Notifier)."""

    async def send(self, text: str, *, kind: str) -> None: ...
    async def request_approval(self, proposal_id: str, text: str, code: str,
                               timeout_s: int) -> ApprovalResponse: ...


@dataclass
class Candidate:
    """Closest candidate per window for the NT-1 'no qualifying setup' log."""

    setup: SetupTriggered
    reason: str
    item: str
    depth: int


@dataclass
class EngineStats:
    setups: int = 0
    proposals: int = 0
    reviews: int = 0
    approvals: int = 0
    submitted: int = 0
    rejected: int = 0
    messages: list[str] = field(default_factory=list)


class TradingEngine:
    def __init__(self, cfg: Config, *, now: Callable[[], datetime], calendar: MarketCalendar,
                 macro: MacroCalendar, broker: Broker, journal: SqlJournal, llm: LLMClient,
                 notifier: ApprovalNotifier, history: HistoricalData | None = None,
                 paper_broker: Broker | None = None, require_quotes: bool = True,
                 reports_dir: str | Path = "reports", sim_mode: bool = False) -> None:
        self.cfg = cfg
        self.now = now
        self.cal = calendar
        self.macro = macro
        self.broker = broker
        self.journal = journal
        self.llm = llm
        self.notifier = notifier
        self.history = history
        self.sim_mode = sim_mode
        self.windows = TradingWindows(cfg, calendar, macro)
        symbols = list(cfg.universe.tickers) + list(cfg.universe.context_symbols)
        self.md = MarketDataAgent(symbols, cfg.data.stale_after_sec)
        self.ta = TechnicalAgent(cfg, self.md, calendar, macro, require_quotes=require_quotes)
        self.universe = UniverseAgent(cfg, journal, now)
        self.strategy = StrategyAgent(cfg, llm, journal, now)
        self.reviewer = ReviewAgent(cfg, llm, journal, now)
        self.compliance = ComplianceGuard(cfg, calendar, journal)
        self.risk = RiskManager(cfg, self.windows)
        self.exec = ExecutionAgent(cfg, broker, journal, paper_broker)
        self.pm = PositionMonitor(cfg, self.windows, calendar)
        self.sup = Supervisor(cfg, calendar, macro)
        self.reporter = Reporter(cfg, journal, llm, self.compliance, reports_dir, now)
        self.bus = EventBus(journal, now)
        self.auth = CommandAuthority(now, journal)
        self.scheduler = Scheduler(calendar)
        self.stats = EngineStats()
        self.day: date | None = None
        self.equity: Decimal = D0
        self.buying_power: Decimal = D0
        self._buffer: list[SetupTriggered] = []
        self._candidates: dict[str, Candidate] = {}
        self._window_orders: dict[str, int] = {"morning": 0, "afternoon": 0}
        self._pending_approval: set[str] = set()
        self._approval_waiters: dict[str, asyncio.Future[str]] = {}
        self._exit_orders: dict[str, tuple[str, datetime, str]] = {}   # order -> (pos, ts, reason)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._eval_lock = asyncio.Lock()
        self._last_mark: datetime | None = None
        self._last_equity_row: datetime | None = None
        self.entries_enabled = True
        self._setup_jobs()

    # ===================================================================================
    # lifecycle
    # ===================================================================================
    def _setup_jobs(self) -> None:
        sch = self.scheduler
        sch.add("preflight", time(7, 0), self.job_preflight)
        sch.add("levels", time(8, 0), self.job_levels)
        sch.add("morning_plan", time(9, 0), lambda now: self.job_plan(now, "morning"))
        sch.add("premarket_levels", time(9, 30), lambda now: self.ta.refresh_premarket())
        sch.add("morning_no_trade_log", time(10, 16), lambda now: self.job_window_end(
            now, "morning"))
        sch.add("morning_summary", time(10, 35), lambda now: self.job_report(now, "morning"))
        sch.add("afternoon_plan", time(14, 45), lambda now: self.job_plan(now, "afternoon"))
        sch.add("year_end_check", time(15, 45), self.job_year_end)
        sch.add("afternoon_no_trade_log", time(15, 51), lambda now: self.job_window_end(
            now, "afternoon"))
        sch.add("end_of_day", time(16, 0), self.job_end_of_day)
        sch.add("daily_review", time(16, 30), lambda now: self.job_report(now, "daily"))
        sch.add("weekly_review", time(17, 0), self.job_weekly, when="friday")
        sch.add("monthly_review", time(17, 30), self.job_monthly, when="month_end")

    async def start(self) -> None:
        """Rebuild state from the journal, then reconcile before accepting entries
        (ARCHITECTURE §3, §6)."""
        now = self.now()
        self.compliance.load_from_journal(now)
        self._restore_positions()
        self._restore_switches()
        self.universe.record(et_date(now))
        await self._refresh_account()
        self.risk.state.hwm = max(self.risk.state.hwm, self._journal_hwm(), self.equity)
        problems = await self.reconcile()
        if problems:
            log.warning("start_reconcile_mismatch", problems=problems)

    def _journal_hwm(self) -> Decimal:
        row = self.journal.last_equity()
        return Decimal(str(row["hwm"])) if row and row.get("hwm") is not None else D0

    def _restore_switches(self) -> None:
        inc = self.journal.rows(s.incidents, order_by="id")
        for r in inc:
            if r["type"] == "KILL_SWITCH" and r["resolved_at"] is None:
                self.risk.state.kill_switch = self.sup.kill_switch = True
            if r["type"] == "HALT_LL6" and r["resolved_at"] is None:
                self.risk.state.live_disabled = True
        for r in self.journal.closed_positions():
            self.risk.state.history.append(ClosedTrade(
                position_id=r["position_id"], ticker=r["ticker"], setup=r["setup"] or "",
                style=r["style"] or "day", window=r["window"] or "morning",
                closed_at=r["closed_at"], pnl=r["pnl"] or D0,
                r_multiple=float(r["r_multiple"] or 0), exit_reason=r["exit_reason"] or "",
                route=r["route"] or "broker"))

    def _restore_positions(self) -> None:
        from trader.agents.a09_positions import Leg

        for r in self.journal.open_positions():
            st = r.get("state_json") or {}
            if not st:
                continue
            p = ManagedPosition(
                position_id=r["position_id"], ticker=r["ticker"], style=r["style"],
                window=r["window"], setup=r["setup"], planned=bool(r["planned"]),
                route=r["route"] or "broker", override=bool(r["override"]),
                trigger=Decimal(st["trigger"]), breakout_level=float(st["breakout_level"]),
                atr5=Decimal(st["atr5"]), initial_stop=Decimal(st["initial_stop"]),
                stop=Decimal(st["stop"]), t1=Decimal(st["t1"]), t2=Decimal(st["t2"]),
                opened_at=r["opened_at"], opened_day=et_date(r["opened_at"]),
                qty_filled=int(st["qty_filled"]), cost=Decimal(st["cost"]),
                t1_hit=bool(st["t1_hit"]), max_high=float(st["max_high"]),
                exit_qty=int(st["exit_qty"]), exit_value=Decimal(st["exit_value"]),
                stop_kind=st["stop_kind"])
            for name, lg in st.get("legs", {}).items():
                p.legs[name] = Leg(name=name, entry_order_id=lg["entry_order_id"],
                                   qty=lg["qty"], open_qty=lg["open_qty"],
                                   tp_price=Decimal(lg["tp_price"]), tp_id=lg["tp_id"],
                                   sl_id=lg["sl_id"])
                for oid, role in ((lg["entry_order_id"], "entry"), (lg["tp_id"], "take_profit"),
                                  (lg["sl_id"], "stop_loss")):
                    if oid:
                        self.pm.by_order[oid] = (p.position_id, name, role)
            self.pm.positions[p.position_id] = p

    async def begin_day(self, d: date) -> None:
        self.day = d
        close = self.cal.session(d).close.time() if self.cal.is_trading_day(d) else time(16, 0)
        self.md.start_day(d, close)
        self.ta.start_day(d)
        self._window_orders = {"morning": 0, "afternoon": 0}
        self._candidates = {}
        await self._refresh_account()
        self.risk.start_day(d, self.equity)
        self.risk.state.kill_switch = self.sup.kill_switch
        self.risk.state.paused_styles = set(self.sup.paused)
        await self.job_levels(self.now())
        acts = self.pm.start_day(d, self.ta.levels)
        await self.execute_actions(acts)
        self.journal.snapshot_config(d, self.now(), _cfg_hash(self.cfg), "")

    # ===================================================================================
    # scheduled jobs
    # ===================================================================================
    async def job_preflight(self, now: datetime) -> None:
        ok, problems = self.sup.preflight(et_date(now))
        rs = self.risk.state
        text = (f"Pre-flight {to_et(now):%a %b %d} — {'GREEN' if ok else 'RED'}\n"
                + ("All checks green/amber" if ok else "NO TRADING TODAY: " + "; ".join(problems))
                + f"\nEquity ${self.equity:,.2f} (HWM ${rs.hwm:,.2f}) · risk/trade "
                f"{self.risk.risk_pct(True)}%")
        if not ok:
            self.entries_enabled = False
            self.journal.incident(now, "PREFLIGHT_RED", "; ".join(problems))
        else:
            self.entries_enabled = True
        locks = self.compliance.locked_list(now)
        if locks:
            text += "\nLocked: " + ", ".join(f"{x['ticker']} ({x['type']} to "
                                              f"{x['until'].astimezone(ET):%b %d})"
                                              for x in locks)
        await self.say(text, "preflight")

    async def job_levels(self, now: datetime) -> None:
        """A4-1 level map at 8:00 from daily bars before today (no look-ahead)."""
        d = et_date(now)
        if self.history is None:
            return
        hist_days: dict[str, int] = {}
        for t in list(self.cfg.universe.tickers) + list(self.cfg.universe.context_symbols):
            try:
                daily = await self.history.daily_bars(t, d - timedelta(days=200), d)
            except Exception as exc:   # noqa: BLE001
                log.warning("levels_history_failed", ticker=t, error=str(exc))
                continue
            st = self.md.state.get(t)
            lv = compute_levels(t, d, daily, st.pm_high if st else None,
                                st.pm_low if st else None)
            hist_days[t] = lv.history_days if lv else 0
            if lv is not None:
                self.ta.set_levels(lv)
                if st is not None:
                    st.prior_close = lv.prior_close
                self.journal.insert(s.levels, {
                    "date": d, "ticker": t, "prior_high": lv.prior_high,
                    "prior_low": lv.prior_low, "prior_close": lv.prior_close,
                    "pm_high": lv.pm_high, "pm_low": lv.pm_low, "ema20d": lv.ema20d,
                    "ema50d": lv.ema50d, "atr14d": lv.atr14d,
                    "json": lv.model_dump(mode="json")})
        # UR-3: history check (tradability is checked against the broker in live pre-flight)
        need = self.cfg.universe.min_history_days
        for t in self.cfg.universe.tickers:
            if hist_days.get(t, 0) < need:
                self.universe.blocked[t] = f"{t}: {hist_days.get(t, 0)} days of history < " \
                                           f"{need} (UR-3)"
            else:
                self.universe.blocked.pop(t, None)

    async def job_plan(self, now: datetime, window: str) -> None:
        """A5-1 + A12-4: game plan with the locked list."""
        d = et_date(now)
        if window == "afternoon" and not self.windows.afternoon_open(d):
            return
        locks = [{"ticker": x["ticker"],
                  "reason": f"{x['type']} until {x['until'].astimezone(ET):%Y-%m-%d}"}
                 for x in self.compliance.locked_list(now)]
        for t, why in self.universe.blocked.items():
            locks.append({"ticker": t, "reason": why})
        data: dict[str, Any] = {"date": d.isoformat(), "tickers": {}, "market": {}}
        for t in self.cfg.universe.tickers:
            lv = self.ta.levels.get(t)
            st = self.md.state[t]
            nf = self.ta.news.get(t)
            data["tickers"][t] = {
                "levels": lv.model_dump(mode="json", exclude={"day", "ticker"}) if lv else None,
                "last": st.last_price, "pm_high": st.pm_high, "pm_low": st.pm_low,
                "gap_pct": round((st.pm_high or st.last_price or 0) / lv.prior_close * 100 - 100,
                                 2) if lv and (st.pm_high or st.last_price) else None,
                "news": {"summary": nf.summary, "catalyst": nf.catalyst, "tone": nf.tone,
                         "flags": list(nf.flags)} if nf else None}
        for c in self.cfg.universe.context_symbols:
            st = self.md.state[c]
            data["market"][c] = {"last": st.last_price, "prior_close": st.prior_close,
                                 "above_vwap": st.above_vwap()}
        plan = await self.strategy.game_plan(d, window, data, locks)
        self.bus.publish(plan)
        rs = self.risk.state
        budget = rs.day_start_equity * Decimal(str(self.cfg.risk.daily_loss_limit_pct)) / 100
        lines = [f"{window.capitalize()} plan {to_et(now):%a %b %d} ({plan.source})",
                 f"Market: {plan.market_bias}"]
        if plan.watch:
            lines.append("Watch (max 4):")
            for i, w in enumerate(plan.watch, 1):
                lines.append(f" {i}. {w.ticker:<5} {w.setup:<14} trigger {w.trigger:.2f} "
                             f"stop {w.stop:.2f} T1 {w.target_1:.2f} — {w.why}")
        else:
            lines.append("Watch: none (zero picks is normal)")
        if plan.off_limits:
            lines.append("Off-limits: " + ", ".join(f"{t} ({r})" for t, r in plan.off_limits))
        left = self.cfg.risk.max_trades_per_day - rs.trades_total_today
        lines.append(f"Trades left today: {left} · Daily loss budget: ${budget:,.0f}")
        await self.say("\n".join(lines), "game_plan")

    async def job_window_end(self, now: datetime, window: str) -> None:
        """NT-1: no order in the window -> log 'no qualifying setup' with the closest
        candidate and the item that failed."""
        if self._window_orders.get(window, 0) > 0:
            return
        c = self._candidates.get(window)
        detail = "no setup triggered" if c is None else (
            f"closest: {c.setup.ticker} {c.setup.setup} score {c.setup.score} failed {c.item} "
            f"({c.reason})")
        nt = NoTrade(ts=now, ticker=c.setup.ticker if c else None,
                     setup=c.setup.setup if c else None, reason_code=Reason.NT_NO_SETUP,
                     detail=detail, stage=window)
        self.bus.publish(nt)
        self.journal.insert(s.decisions, {
            "proposal_id": f"window-{et_date(now)}-{window}", "ts": now,
            "ticker": nt.ticker, "setup": nt.setup, "window": window, "style": None,
            "result": "NO_TRADE", "reason_code": Reason.NT_NO_SETUP, "qty": 0,
            "risk_usd": D0, "checklist_json": [], "detail": detail})
        await self.say(f"No trade in the {window} window: no qualifying setup. {detail}",
                       "no_trade")

    async def job_report(self, now: datetime, kind: str) -> None:
        try:
            rep = await self.reporter.daily(et_date(now), kind)
            await self.say(rep["text"], f"report_{kind}")
        except JournalUnavailable as exc:
            log.error("report_failed", error=str(exc))

    async def job_weekly(self, now: datetime) -> None:
        rep = await self.reporter.weekly(et_date(now))
        await self.say(rep["text"], "report_weekly")

    async def job_monthly(self, now: datetime) -> None:
        d = et_date(now)
        rep = await self.reporter.monthly(d.year, d.month)
        await self.say(rep["text"], "report_monthly")

    async def job_year_end(self, now: datetime) -> None:
        """WS-5: no open position carries a deferred wash-sale loss into the new year."""
        d = et_date(now)
        if self.cal.next_trading_day(d).year == d.year:
            return
        must = set(self.compliance.year_end_must_close(now))
        acts: list[Action] = [self.pm._exit(p, Reason.EXIT_YEAR_END_WASH)
                              for p in self.pm.open_positions()
                              if self.cfg.canonical(p.ticker) in must and not p.pending_exit]
        await self.execute_actions(acts)

    async def job_end_of_day(self, now: datetime) -> None:
        await self._refresh_account()
        self._record_equity(now, force=True)
        end_of_day = getattr(self.broker, "end_of_day", None)
        if self.sim_mode and callable(end_of_day):
            end_of_day()

    # ===================================================================================
    # market events
    # ===================================================================================
    async def on_bar(self, bar: Bar) -> None:
        now = self.now()
        if self.day is None or et_date(bar.ts) != self.day:
            if self.cal.is_trading_day(et_date(bar.ts)) and to_et(bar.ts).time() >= time(9, 30):
                await self.tick(now)
        closed5 = self.md.on_bar(bar, received_at=now)
        actions: list[Action] = list(self.pm.on_1m(bar, now))
        setups: list[SetupTriggered] = []
        if bar.symbol in self.cfg.universe.tickers:
            setups += self.ta.on_1m_close(bar, now)
        for b5 in closed5:
            actions += self._on_5m(b5, now)
            if b5.symbol in self.cfg.universe.tickers:
                setups += self.ta.on_5m_close(b5, now)
        await self.execute_actions(actions)
        self._buffer += setups

    def _on_5m(self, b5: Bar, now: datetime) -> list[Action]:
        st = self.md.state[b5.symbol]
        return self.pm.on_5m(b5, now, st.vwap_value, st.ema9_5m.value, st.atr5.value,
                             self.ta.levels.get(b5.symbol), st.bars5)

    def on_quote(self, q: Quote) -> None:
        self.md.on_quote(q)

    def on_status(self, st: TradingStatus) -> None:
        self.md.on_status(st)
        if st.kind == "halt":
            self.journal.incident(st.ts, "HALT", f"{st.symbol} {st.detail}")

    def on_news(self, flags: EventFlags) -> None:
        t = self.cfg.canonical(flags.ticker)
        self.ta.news[t] = flags
        self.compliance.news[t] = flags
        self.pm.news[t] = flags

    async def tick(self, now: datetime | None = None) -> None:
        """Clock tick: schedule, 5-min flush, flatten times, cancels, limits, setups."""
        now = now or self.now()
        d = et_date(now)
        if self.day != d and self.cal.is_trading_day(d) and to_et(now).time() >= time(4, 0):
            await self.begin_day(d)
        await self.scheduler.fire_due(now)
        if self.day != d:
            return
        for b5 in self.md.flush(now, grace_s=0 if self.sim_mode else 5):
            await self.execute_actions(self._on_5m(b5, now))
            if b5.symbol in self.cfg.universe.tickers:
                self._buffer += self.ta.on_5m_close(b5, now)
        await self.execute_actions(self.pm.on_tick(now))
        await self._check_exit_orders(now)
        last = {sym: st.last_price for sym, st in self.md.state.items()
                if st.last_price is not None}
        cutoff = et_datetime(d, self.windows.a_swing_until) + timedelta(minutes=5)
        for pid, reason in await self.exec.check_cancels(now, last, cutoff):
            self.journal.insert(s.decisions, {"proposal_id": pid, "ts": now, "result": "CANCEL",
                                              "reason_code": reason, "qty": 0,
                                              "risk_usd": D0, "checklist_json": [],
                                              "detail": "unfilled entry cancelled (EX-3)"})
        await self._mark(now)
        if self.exec.failures >= 3:
            self.sup.set_health("alpaca", "red", "3 consecutive broker failures", now)
        if self._buffer:
            buf, self._buffer = sorted(self._buffer, key=lambda x: -x.score), []
            if self.sim_mode:   # replay: inline, deterministic
                for setup in buf:
                    await self.evaluate_setup(setup)
            else:   # live: Qwen calls must never delay flatten times, exits or cancels
                self._spawn(self._evaluate_batch(buf))

    async def _evaluate_batch(self, buf: list[SetupTriggered]) -> None:
        async with self._eval_lock:   # one pipeline at a time: limits see every prior decision
            for setup in buf:
                try:
                    await self.evaluate_setup(setup)
                except Exception as exc:   # noqa: BLE001 - one bad setup must not stop the rest
                    log.exception("evaluate_failed", setup=setup.setup_id, error=str(exc))
                    self.journal.incident(self.now(), "PIPELINE_ERROR", str(exc)[:500])

    async def _mark(self, now: datetime) -> None:
        if self._last_mark is not None and now - self._last_mark < timedelta(seconds=30):
            return
        self._last_mark = now
        if self.sim_mode or self._last_equity_row is None or now - self._last_equity_row >= \
                timedelta(minutes=1):
            await self._refresh_account()
        open_pnl = D0
        for p in self.pm.open_positions():
            last = self.md.state[p.ticker].last_price if p.ticker in self.md.state else None
            if last is not None and p.route == "broker":
                open_pnl += (Decimal(str(last)) - p.avg_entry) * p.qty_open
        hits = self.risk.mark(now, self.equity, open_pnl)
        await self.handle_limits(hits)
        self._record_equity(now)

    def _record_equity(self, now: datetime, force: bool = False) -> None:
        if not force and self._last_equity_row is not None and now - self._last_equity_row < \
                timedelta(minutes=5):
            return
        self._last_equity_row = now
        self.journal.insert(s.equity, {"ts": now, "equity": self.equity,
                                       "hwm": max(self.risk.state.hwm, self.equity)})

    async def _refresh_account(self) -> None:
        try:
            acct = await self.broker.account()
        except (BrokerUnavailable, BrokerRejected) as exc:
            log.warning("account_failed", error=str(exc))
            return
        self.equity, self.buying_power = acct.equity, acct.buying_power

    # ===================================================================================
    # the decision pipeline
    # ===================================================================================
    def context(self, s_: SetupTriggered, now: datetime, planned: bool) -> DecisionContext:
        st = self.md.state[s_.ticker]
        blocked, why = self.sup.entries_blocked()
        if not self.entries_enabled:
            blocked, why = True, "pre-flight red"
        positions = [OpenPositionView(
            position_id=p.position_id, ticker=p.ticker, group=self.cfg.canonical(p.ticker),
            style=p.style, window=p.window, qty=p.qty_open, entry=p.avg_entry, stop=p.stop,
            last=Decimal(str(self.md.state[p.ticker].last_price or p.avg_entry)),
            t1_hit=p.t1_hit, route=p.route) for p in self.pm.open_positions()]
        live = Decimal(str(st.last_price or float(s_.entry)))
        filters = self.ta.filters(s_.ticker, now, s_.setup, s_.score)
        return DecisionContext(
            now=now, equity=self.equity, buying_power=self.buying_power, last_price=live,
            positions=positions, filters=filters, planned=planned, health_ok=not blocked,
            health_detail=why, data_stale=self.md.is_stale(now) or st.incomplete,
            news_available=self.sup.news_ok() and self.ta.news_available,
            approvals_available=self.sup.notifier_ok())

    def _note_candidate(self, s_: SetupTriggered, items: list[ChecklistItem], reason: str
                        ) -> None:
        failed = next((i for i in items if not i.passed), None)
        depth = sum(1 for i in items if i.passed)
        c = self._candidates.get(s_.window)
        if c is None or depth > c.depth or (depth == c.depth and s_.score > c.setup.score):
            self._candidates[s_.window] = Candidate(s_, reason, failed.id if failed else "-",
                                                    depth)

    def _journal_decision(self, d: Decision, s_: SetupTriggered) -> None:
        self.journal.insert(s.decisions, {
            "proposal_id": d.proposal_id, "ts": self.now(), "ticker": s_.ticker,
            "setup": s_.setup, "window": s_.window, "style": s_.style, "result": d.result,
            "reason_code": d.reason_code, "qty": d.qty, "risk_usd": d.risk_usd,
            "checklist_json": [i.model_dump() for i in d.checklist], "detail": d.detail,
            "override": d.override})
        self.bus.publish(d)

    def _reject(self, s_: SetupTriggered, pid: str, code: str, detail: str,
                items: list[ChecklistItem] | None = None) -> None:
        d = Decision(proposal_id=pid, result="REJECT", reason_code=code,
                     checklist=tuple(items or ()), detail=detail, entry=s_.entry, stop=s_.stop,
                     t1=s_.t1, t2=s_.t2)
        self._journal_decision(d, s_)
        self._note_candidate(s_, list(items or ()), code)
        self.stats.rejected += 1

    async def evaluate_setup(self, s_: SetupTriggered) -> Decision | None:
        """Run one triggered setup through the full pipeline."""
        now = self.now()
        self.stats.setups += 1
        pid = proposal_id_for(s_.setup_id)
        self.journal.upsert(s.setups, {"id": s_.setup_id}, {
            "ts": s_.ts, "ticker": s_.ticker, "style": s_.style, "window": s_.window,
            "setup_name": s_.setup, "trigger": s_.trigger, "entry": s_.entry, "stop": s_.stop,
            "t1": s_.t1, "t2": s_.t2, "score": s_.score, "filters_json": s_.filters,
            "context_json": {k: v for k, v in s_.context.items()}})
        self.bus.publish(s_)
        if self.exec.seen(pid) or pid in self._pending_approval:
            return None   # duplicate event: one order set only
        ok_u, why_u = self.universe.check(s_.ticker)
        if not ok_u:
            self._reject(s_, pid, Reason.NT_UNIVERSE, why_u)
            return None
        planned = self.strategy.planned(s_.ticker, s_.setup, et_date(now), s_.window)
        ctx = self.context(s_, now, planned)
        comp = self.compliance.check_entry(s_.ticker, now, s_.style, pid)
        pre = self.risk._items(s_, ctx, None, comp)[:8]
        failed = next((i for i in pre if not i.passed), None)
        if failed is not None:
            code = failed.detail.split(":")[0] if failed.detail else f"NT_{failed.id}"
            self._reject(s_, pid, code, f"{failed.id} {failed.detail}", pre)
            return None
        # A5 proposal (Qwen)
        data = self._llm_data(s_, ctx, planned)
        out = await self.strategy.propose(s_, data, planned, float(ctx.last_price))
        self.stats.proposals += 1
        if out.proposal is None:
            self._reject(s_, pid, Reason.NT_LLM_INVALID if out.error and not out.error.startswith(
                ("ticker", "setup", "price", "trigger", "entry", "stop", "target", "reward",
                 "confidence")) else Reason.NT_INVALID_PROPOSAL, f"A5: {out.error}", pre)
            return None
        prop = out.proposal
        self.bus.publish(prop)
        if prop.action == "skip":
            self._reject(s_, pid, Reason.NT_LLM_SKIP, f"A5 skip: {prop.weakest_point}", pre)
            return None
        # A6 review (Qwen)
        review, err = await self.reviewer.review(prop, data)
        self.stats.reviews += 1
        if review is None:
            self._reject(s_, pid, Reason.NT_LLM_INVALID, f"A6: {err} (QW-3: no trade "
                         "without a review)", pre)
            return None
        self.bus.publish(review)
        # A12 + A7 on fresh live data
        ctx = self.context(s_, self.now(), planned)
        comp = self.compliance.check_entry(s_.ticker, self.now(), s_.style, pid)
        self.bus.publish(comp)
        dec = self.risk.evaluate(s_, ctx, review, comp, pid)
        self._journal_decision(dec, s_)
        if dec.result == "REJECT":
            self._note_candidate(s_, list(dec.checklist), dec.reason_code)
            self.stats.rejected += 1
            return dec
        if dec.result == "NEEDS_APPROVAL":
            self._pending_approval.add(pid)
            self._spawn(self._approval_flow(s_, prop, dec, review.verdict, planned))
            return dec
        await self._submit(dec, s_, planned)
        return dec

    def _llm_data(self, s_: SetupTriggered, ctx: DecisionContext, planned: bool
                  ) -> dict[str, Any]:
        """PR-5: summaries, levels and indicators only; never raw bar history or secrets."""
        st = self.md.state[s_.ticker]
        nf = self.ta.news.get(s_.ticker)
        lv = self.ta.levels.get(s_.ticker)
        qqq = self.md.state.get("QQQ")
        rs = self.risk.state
        return {
            "setup": {"ticker": s_.ticker, "setup": s_.setup, "style": s_.style,
                      "window": s_.window, "trigger": float(s_.trigger),
                      "entry": float(s_.entry), "stop": float(s_.stop), "t1": float(s_.t1),
                      "t2": float(s_.t2), "score": s_.score, "filters": s_.filters,
                      "decision_time_et": to_et(s_.ts).strftime("%H:%M")},
            "live": {"last": float(ctx.last_price), "vwap": round(st.vwap_value or 0, 2),
                     "atr5": round(s_.atr5, 3), "rvol": round(st.rvol() or 0, 2),
                     "rel_strength_vs_qqq_pct": round(self.ta.rel_strength(st) or 0, 2),
                     "hod": st.hod, "resistance": s_.resistance,
                     "qqq_above_vwap": qqq.above_vwap() if qqq else None},
            "levels": lv.model_dump(mode="json", exclude={"day", "ticker"}) if lv else None,
            "news": {"summary": nf.summary, "catalyst": nf.catalyst, "tone": nf.tone,
                     "flags": list(nf.flags)} if nf else {"summary": "", "catalyst": "none",
                                                           "tone": "neutral", "flags": []},
            "planned": planned,
            "today": {"pnl": float(rs.day_pnl), "trades": rs.trades_total_today},
        }

    async def _approval_flow(self, s_: SetupTriggered, prop: TradeProposal, dec: Decision,
                             verdict: str, planned: bool) -> None:
        pid = dec.proposal_id
        try:
            timeout = self.cfg.notifications.approval_timeout_sec
            expires = min(prop.expires_at, self.now() + timedelta(seconds=timeout))
            code = self.auth.issue_approval_code(pid, expires)
            self.journal.insert(s.approvals, {"proposal_id": pid, "code": code,
                                              "sent_at": self.now()})
            text = (f"APPROVE? {s_.ticker} {s_.setup} ({'planned' if planned else 'unplanned'})"
                    f"  code {code}\nBuy {dec.qty} @ ≤{dec.entry}  stop {dec.stop}  T1 {dec.t1} "
                    f"({dec.qty // 2})  T2 {dec.t2} ({dec.qty - dec.qty // 2})\n"
                    f"Risk ${dec.risk_usd:.0f} "
                    f"({dec.risk_usd / max(self.equity, Decimal(1)) * 100:.2f}%)"
                    f" · conf {prop.confidence:.2f} · review: {verdict}\nThesis: {prop.thesis}\n"
                    f"Reply YES {code} or NO {code} within {timeout}s. No reply = no trade.")
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._approval_waiters[pid] = fut
            resp = await self.notifier.request_approval(pid, self.sup.mode_tag() + " " + text,
                                                        code, timeout)
            answer: str = resp.answer
            if answer == "timeout" and fut.done():
                answer = fut.result()
            self.journal.update(s.approvals, {"proposal_id": pid},
                                {"answered_at": self.now(), "answer": answer})
            self.stats.approvals += 1
            if answer != "yes":
                code_ = Reason.NT_APPROVAL_REJECTED if answer == "no" else \
                    Reason.NT_APPROVAL_TIMEOUT
                self._reject(s_, pid, code_, f"owner answer: {answer}")
                return
            if self.now() > prop.expires_at:
                self._reject(s_, pid, Reason.NT_EXPIRED, "proposal expired (A5-4)")
                return
            # re-run the full checklist on live data right before submitting
            ctx = self.context(s_, self.now(), planned)
            comp = self.compliance.check_entry(s_.ticker, self.now(), s_.style, pid)
            from trader.models.messages import Review as _Review
            review = _Review(proposal_id=pid, verdict="agree", main_risk="owner approved")
            final = self.risk.evaluate(s_, ctx, review, comp, pid)
            if final.result == "REJECT":
                self._journal_decision(final, s_)
                return
            final = final.model_copy(update={"result": "APPROVE", "reason_code": Reason.APPROVED,
                                             "override": dec.override or verdict != "agree"})
            self._journal_decision(final, s_)
            await self._submit(final, s_, planned)
        finally:
            self._pending_approval.discard(pid)
            self._approval_waiters.pop(pid, None)

    async def _submit(self, dec: Decision, s_: SetupTriggered, planned: bool) -> None:
        now = self.now()
        if self.risk.state.kill_switch:
            self._reject(s_, dec.proposal_id, Reason.NT_KILL_SWITCH, "kill switch on at submit")
            return
        code, ticket = await self.exec.execute(dec, s_, now, planned)
        if code == Reason.EXEC_RATE_LIMIT:
            self.journal.incident(now, "RULE_BREACH", "order-rate guard tripped (SEC-12)")
            await self.kill("order-rate guard (SEC-12)", by="A8")
            return
        if code != Reason.EXEC_SUBMITTED or ticket is None:
            self.journal.incident(now, "EXEC_FAILED", f"{dec.proposal_id} {code}")
            return
        self.risk.on_entry_submitted(s_)
        self._window_orders[s_.window] = self._window_orders.get(s_.window, 0) + 1
        self.stats.submitted += 1
        self._sim_route_children(ticket.proposal_id)
        await self.say(f"SUBMITTED {s_.ticker} {s_.setup} {dec.qty} @ ≤{dec.entry} stop {dec.stop}"
                       f" T1 {dec.t1} T2 {dec.t2}" + (" [paper]" if dec.route == "paper" else ""),
                       "trade")

    def _sim_route_children(self, proposal_id: str) -> None:
        """No-op hook (children are mapped from order updates)."""

    # ===================================================================================
    # orders and positions
    # ===================================================================================
    async def on_order_update(self, u: OrderUpdate) -> None:
        o = u.order
        if o.leg in ("take_profit", "stop_loss") and o.parent_id:
            if o.id not in self.pm.by_order:
                self.pm.register_child(o.parent_id, o.id, o.leg)
        if u.event in ("fill", "partial_fill") and u.fill_qty > 0 and u.fill_price is not None:
            self.journal.insert(s.fills, {"broker_id": o.id, "client_order_id": o.client_order_id,
                                          "ticker": o.symbol, "side": o.side, "ts": u.ts,
                                          "qty": u.fill_qty, "price": u.fill_price})
            if o.leg == "entry":
                await self._on_entry_fill(o.id, u.fill_qty, u.fill_price, u.ts)
            else:
                await self._on_exit_fill(o.id, u.fill_qty, u.fill_price, u.ts)
        elif u.event in ("canceled", "expired", "rejected") and o.leg == "entry":
            key = self.exec.order_index.get(o.id)
            if key:
                t = self.exec.tickets[key[0]]
                t.orders[key[1]] = o
                if all(t.filled.get(k, 0) >= v.qty or v.status in ("canceled", "expired",
                                                                   "rejected")
                       for k, v in t.orders.items()):
                    t.done = True
        elif u.event in ("canceled", "expired") and o.id in self._exit_orders:
            pass   # handled by _check_exit_orders (re-submit)

    async def _on_entry_fill(self, order_id: str, qty: int, price: Decimal, ts: datetime
                             ) -> None:
        ticket = self.exec.on_entry_fill(order_id, qty)
        if ticket is None:
            return
        leg = self.exec.order_index[order_id][1]
        p, new = self.pm.on_entry_fill(ticket, leg, order_id, qty, price, ts)
        p.reduced_size = self.risk.state.reduced_since is not None
        self.compliance.on_buy_fill(p.position_id, p.ticker, qty, price, ts, p.route)
        children = getattr(self.exec.broker_for(p.route), "children_of", None)
        if callable(children):
            for ch in children(order_id):
                self.pm.register_child(order_id, ch.id, ch.leg)
        if new:
            self.bus.publish(PositionOpened(position_id=p.position_id, ticker=p.ticker,
                                            style=p.style, qty=qty, avg_entry=price,
                                            stop=p.stop, ts=ts))
            self.journal.insert(s.positions, {
                "position_id": p.position_id, "ticker": p.ticker, "style": p.style,
                "window": p.window, "setup": p.setup, "planned": p.planned, "opened_at": ts,
                "qty": p.qty_filled, "avg_entry": p.avg_entry, "initial_stop": p.initial_stop,
                "risk_usd": p.r_per_share * p.qty_filled, "route": p.route,
                "override": p.override, "state_json": self._pos_state(p)})
            self.compliance.check_loss_exit(p.position_id, p.ticker, ts)   # A12-2 at entry
            slip = price - ticket.entry
            await self.say(f"FILLED {p.ticker} {qty} @ {price} (slip {slip:+.2f}) · stops live "
                           "at broker", "trade")
        else:
            self._persist(p)

    async def _on_exit_fill(self, order_id: str, qty: int, price: Decimal, ts: datetime
                            ) -> None:
        pos_id = self._exit_orders.get(order_id, (None,))[0]
        p, event, acts = self.pm.on_exit_fill(order_id, qty, price, ts, pos_id)
        if p is None:
            log.warning("unknown_exit_fill", order=order_id)
            return
        self.compliance.on_sell_fill(p.position_id, p.ticker, qty, price, ts,
                                     p.r_multiple() if p.closed else 0.0)
        if event == Reason.EXIT_T1:
            r = (price - p.avg_entry) / p.r_per_share if p.r_per_share else D0
            await self.say(f"T1 {p.ticker} {qty} @ {price} ({r:+.1f}R)", "trade")
        await self.execute_actions(acts)
        if p.closed:
            await self._finalize(p, ts)
        else:
            self._persist(p)

    async def _finalize(self, p: ManagedPosition, ts: datetime) -> None:
        pnl, r = p.pnl(), p.r_multiple()
        reason = p.exit_reason or p.pending_exit or Reason.EXIT_FLATTEN_CMD
        avg_exit = (p.exit_value / p.exit_qty).quantize(Decimal("0.0001")) if p.exit_qty else D0
        # cancel any leftover child orders
        broker = self.exec.broker_for(p.route)
        for lg in p.legs.values():
            for oid in (lg.tp_id, lg.sl_id):
                if oid:
                    with contextlib.suppress(BrokerUnavailable, BrokerRejected):
                        await broker.cancel(oid)
        self.journal.update(s.positions, {"position_id": p.position_id}, {
            "closed_at": ts, "qty": p.qty_filled, "avg_entry": p.avg_entry,
            "avg_exit": avg_exit, "pnl": pnl.quantize(Decimal("0.01")),
            "r_multiple": round(r, 4), "exit_reason": reason, "state_json": self._pos_state(p)})
        self.bus.publish(PositionClosed(position_id=p.position_id, ticker=p.ticker,
                                        style=p.style, qty=p.qty_filled, avg_entry=p.avg_entry,
                                        avg_exit=avg_exit, pnl=pnl, r_multiple=r,
                                        exit_reason=reason, ts=ts))
        for lk in self.compliance.on_position_closed(p.ticker, pnl, r, ts):
            self.bus.publish(lk)
        hits: list[LimitHit] = []
        if p.route == "broker":
            hits = self.risk.on_trade_closed(ClosedTrade(
                position_id=p.position_id, ticker=p.ticker, setup=p.setup, style=p.style,
                window=p.window, closed_at=ts, pnl=pnl, r_multiple=r, exit_reason=reason,
                route=p.route, reduced_size=p.reduced_size), ts)
        else:
            self.risk.state.history.append(ClosedTrade(
                position_id=p.position_id, ticker=p.ticker, setup=p.setup, style=p.style,
                window=p.window, closed_at=ts, pnl=pnl, r_multiple=r, exit_reason=reason,
                route=p.route))
        self.pm.remove_closed()
        for oid in [k for k, v in self._exit_orders.items() if v[0] == p.position_id]:
            del self._exit_orders[oid]
        await self.say(f"CLOSED {p.ticker} {p.qty_filled} avg {avg_exit:.2f} {reason} · trade "
                       f"{r:+.2f}R, ${pnl:+.2f}", "trade")
        await self._refresh_account()
        self._record_equity(ts, force=True)
        await self.handle_limits(hits)

    def _pos_state(self, p: ManagedPosition) -> dict[str, Any]:
        return {"trigger": str(p.trigger), "breakout_level": p.breakout_level,
                "atr5": str(p.atr5), "initial_stop": str(p.initial_stop), "stop": str(p.stop),
                "t1": str(p.t1), "t2": str(p.t2), "qty_filled": p.qty_filled,
                "cost": str(p.cost), "t1_hit": p.t1_hit, "max_high": p.max_high,
                "exit_qty": p.exit_qty, "exit_value": str(p.exit_value),
                "stop_kind": p.stop_kind,
                "legs": {k: {"entry_order_id": lg.entry_order_id, "qty": lg.qty,
                             "open_qty": lg.open_qty, "tp_price": str(lg.tp_price),
                             "tp_id": lg.tp_id, "sl_id": lg.sl_id} for k, lg in p.legs.items()}}

    def _persist(self, p: ManagedPosition) -> None:
        self.journal.update(s.positions, {"position_id": p.position_id},
                            {"qty": p.qty_filled, "avg_entry": p.avg_entry,
                             "state_json": self._pos_state(p)})

    async def execute_actions(self, actions: list[Action]) -> None:
        for a in actions:
            if isinstance(a, StopMove):
                await self._move_stop(a)
            elif isinstance(a, ExitAction):
                await self._exit(a)

    async def _move_stop(self, mv: StopMove) -> None:
        p = self.pm.positions.get(mv.position_id)
        if p is None:
            return
        old = p.stop
        try:
            self.pm.apply_stop_move(mv)
        except StopWidenError as exc:   # DR-2
            self.journal.incident(self.now(), "RULE_BREACH_BLOCKED", str(exc))
            return
        broker = self.exec.broker_for(p.route)
        for lg in p.legs.values():
            if lg.sl_id and lg.open_qty > 0:
                try:
                    new = await broker.replace_stop(lg.sl_id, mv.new_stop)
                except (BrokerUnavailable, BrokerRejected) as exc:
                    log.error("replace_stop_failed", order=lg.sl_id, error=str(exc))
                    continue
                if new.id != lg.sl_id:   # Alpaca replaces create a new order id
                    self.pm.by_order[new.id] = self.pm.by_order.get(
                        lg.sl_id, (p.position_id, lg.name, "stop_loss"))
                    lg.sl_id = new.id
        self.bus.publish(StopMoved(position_id=p.position_id, ticker=p.ticker, old_stop=old,
                                   new_stop=mv.new_stop, reason=mv.reason, ts=self.now()))
        self._persist(p)

    async def _exit(self, a: ExitAction) -> None:
        p = self.pm.positions.get(a.position_id)
        if p is None or p.qty_open <= 0:
            return
        now = self.now()
        st = self.md.state.get(p.ticker)
        last = st.last_price if st and st.last_price else float(p.avg_entry)
        if Decimal(str(last)) < p.avg_entry:   # A12-2 look-forward before a loss exit
            self.compliance.check_loss_exit(p.position_id, p.ticker, now)
        broker = self.exec.broker_for(p.route)
        for lg in p.legs.values():
            for oid in (lg.tp_id, lg.sl_id):
                if oid and lg.open_qty > 0:
                    with contextlib.suppress(BrokerUnavailable, BrokerRejected):
                        await broker.cancel(oid)
        bid = st.quote.bid if st and st.quote else None
        limit = marketable_exit_price(bid, last)
        try:
            order = await broker.close_position(p.ticker, p.qty_open, limit)
        except (BrokerUnavailable, BrokerRejected) as exc:
            log.error("exit_failed", ticker=p.ticker, error=str(exc))
            self.journal.incident(now, "EXIT_FAILED", f"{p.ticker} {a.reason}: {exc}")
            p.pending_exit = None   # retry on the next tick
            return
        self._exit_orders[order.id] = (p.position_id, now, a.reason)
        self.pm.by_order[order.id] = (p.position_id, "", "exit")
        self.journal.insert(s.orders, {
            "client_order_id": order.client_order_id, "broker_id": order.id,
            "proposal_id": p.position_id, "leg": "exit", "type": "limit", "side": "sell",
            "ticker": p.ticker, "qty": p.qty_open, "limit": limit, "status": order.status,
            "ts": now, "route": p.route})

    async def _check_exit_orders(self, now: datetime) -> None:
        """An exit that has not filled within a minute is re-priced (stops were cancelled)."""
        for oid, (pid, ts, reason) in list(self._exit_orders.items()):
            p = self.pm.positions.get(pid)
            if p is None or p.closed:
                self._exit_orders.pop(oid, None)
                continue
            if now - ts < timedelta(seconds=60):
                continue
            broker = self.exec.broker_for(p.route)
            with contextlib.suppress(BrokerUnavailable, BrokerRejected):
                await broker.cancel(oid)
            self._exit_orders.pop(oid, None)
            p.pending_exit = None
            await self._exit(ExitAction(pid, reason, p.qty_open))

    # ===================================================================================
    # limits, kill switch, commands
    # ===================================================================================
    async def handle_limits(self, hits: list[LimitHit]) -> None:
        for h in hits:
            self.bus.publish(h)
            self.journal.incident(h.ts, h.reason_code, h.detail)
            await self.say(f"HALT: {h.rule} {h.detail}", "alert")
            if h.rule == "LL-3":   # flatten day trades, no more entries today
                await self.execute_actions(self.pm.flatten_all(Reason.EXIT_DAILY_LIMIT, "day"))
                await self.exec.cancel_all_entries(Reason.EXIT_DAILY_LIMIT, h.ts)

    async def kill(self, reason: str, by: str = "owner") -> int:
        """A10-3: cancel open entries, block new ones, keep broker-side stops."""
        now = self.now()
        self.sup.kill_switch = self.risk.state.kill_switch = True
        self.sup.kill_reason = reason
        n = await self.exec.cancel_all_entries(Reason.EXEC_CANCEL_KILL, now)
        self.journal.incident(now, "KILL_SWITCH", f"{by}: {reason}; {n} entries cancelled")
        self.bus.publish(KillSwitch(on=True, by=by, ts=now, reason=reason))
        await self.say(f"KILL SWITCH ON ({by}: {reason}) — {n} entry orders cancelled; "
                       "broker-side stops kept", "alert")
        return n

    async def resume(self, by: str = "owner") -> None:
        now = self.now()
        self.sup.kill_switch = self.risk.state.kill_switch = False
        for r in self.journal.rows(s.incidents, {"type": "KILL_SWITCH"}):
            if r["resolved_at"] is None:
                self.journal.update(s.incidents, {"id": r["id"]}, {"resolved_at": now})
        self.bus.publish(KillSwitch(on=False, by=by, ts=now))
        await self.say("Kill switch off: entries allowed again", "alert")

    async def handle_command(self, cmd: Command) -> str:
        action, arg, reply = self.auth.handle(cmd)
        if action in ("APPROVE", "REJECT_TRADE"):
            fut = self._approval_waiters.get(arg)
            if fut is not None and not fut.done():
                fut.set_result("yes" if action == "APPROVE" else "no")
            resolver = getattr(self.notifier, "resolve", None)
            if callable(resolver):
                resolver(arg, "yes" if action == "APPROVE" else "no")
        elif action == "KILL":
            await self.kill("command", by=cmd.sender)
        elif action == "RESUME":
            if arg.strip() == "LIVE":
                self.risk.state.live_disabled = False
                for r in self.journal.rows(s.incidents, {"type": "HALT_LL6"}):
                    if r["resolved_at"] is None:
                        self.journal.update(s.incidents, {"id": r["id"]},
                                            {"resolved_at": self.now()})
            else:
                await self.resume(by=cmd.sender)
        elif action == "FLATTEN":
            for p in self.pm.open_positions():
                p.override = True   # DR-8: manual action, tagged and reported
            await self.execute_actions(self.pm.flatten_all(Reason.EXIT_FLATTEN_CMD))
            await self.exec.cancel_all_entries(Reason.EXIT_FLATTEN_CMD, self.now())
        elif action == "PAUSE":
            style = "swing" if "SWING" in arg else "day"
            self.sup.paused.add(style)
            self.risk.state.paused_styles.add(style)
        elif action == "UNPAUSE":
            style = "swing" if "SWING" in arg else "day"
            self.sup.paused.discard(style)
            self.risk.state.paused_styles.discard(style)
        elif action == "STATUS":
            reply = self.status_text()
        if cmd.source != "local" or action in ("NEED_CONFIRM", "STATUS"):
            await self.say(reply, "command_reply")
        return reply

    def status_text(self) -> str:
        rs = self.risk.state
        pos = ", ".join(f"{p.ticker} {p.qty_open}sh stop {p.stop}" for p in
                        self.pm.open_positions()) or "none"
        return (f"Status {to_et(self.now()):%H:%M} · health {self.sup.overall()} · kill "
                f"{'ON' if rs.kill_switch else 'off'} · equity ${self.equity:,.2f} · day P&L "
                f"${rs.day_pnl:+.2f} · trades {rs.trades_total_today} · halted "
                f"{rs.halted or 'no'} · positions: {pos}")

    def status_dict(self) -> dict[str, Any]:
        rs = self.risk.state
        return {"ts": self.now().isoformat(), "health": {k: v.level for k, v in
                                                         self.sup.health.items()},
                "kill_switch": rs.kill_switch, "equity": str(self.equity),
                "day_pnl": str(rs.day_pnl), "trades_today": rs.trades_total_today,
                "halted": rs.halted, "paused": sorted(rs.paused_styles),
                "positions": [{"ticker": p.ticker, "qty": p.qty_open, "stop": str(p.stop),
                               "style": p.style} for p in self.pm.open_positions()],
                "locks": [{"ticker": x["ticker"], "type": x["type"]}
                          for x in self.compliance.locked_list(self.now())]}

    # ===================================================================================
    # reconcile and helpers
    # ===================================================================================
    async def reconcile(self) -> list[str]:
        expected: dict[str, int] = {}
        for p in self.pm.open_positions():
            if p.route == "broker":
                expected[p.ticker] = expected.get(p.ticker, 0) + p.qty_open
        problems = await self.exec.reconcile(expected, set(self.pm.by_order) | set(
            self._exit_orders))
        now = self.now()
        if problems:
            self.sup.set_health("reconcile", "red", "; ".join(problems)[:300], now)
            self.journal.incident(now, "RECONCILE_MISMATCH", "; ".join(problems))
        else:
            self.sup.set_health("reconcile", "green", "journal = broker", now)
        return problems

    async def say(self, text: str, kind: str) -> None:
        msg = f"{self.sup.mode_tag()} {text}"
        self.stats.messages.append(msg)
        try:
            await self.notifier.send(msg, kind=kind)
        except Exception as exc:   # noqa: BLE001 - notifications never break trading
            log.warning("notify_failed", error=str(exc))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """Wait for background approval flows (replay/tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def _cfg_hash(cfg: Config) -> str:
    import hashlib

    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()


AsyncFn = Callable[[datetime], Awaitable[None]]
