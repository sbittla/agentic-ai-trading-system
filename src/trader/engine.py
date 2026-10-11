"""TradingEngine: the decision-and-order path inside trader-core (ARCHITECTURE §1, §3, §4).

One engine runs live, paper and replay by swapping interfaces (clock, broker, LLM, notifier,
history). Every step writes to the journal before the next one acts (SR-4). Order of a trade:

    A4 SetupTriggered -> A7 precheck (code, CK-1..8) -> A5 proposal (Qwen) -> A6 review (Qwen)
    -> A12 compliance -> A7 checklist + size -> [owner approval] -> A8 brackets -> A9 exits
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import structlog

from trader.agents import external_trades as ext
from trader.agents import ops as ops_mod
from trader.agents.a01_universe import UniverseAgent
from trader.agents.a02_market_data import MarketDataAgent
from trader.agents.a04_bearish import BearishScanner, BearSignal
from trader.agents.a04_technicals import (
    TechnicalAgent,
    classify_regime,
    compute_levels,
    session_screen,
)
from trader.agents.a05_ideas import alert_text, grade, grade_summary, idea_rows
from trader.agents.a05_strategy import StrategyAgent, proposal_id_for
from trader.agents.a06_review import ReviewAgent
from trader.agents.a07_risk import ClosedTrade, DecisionContext, OpenPositionView, RiskManager
from trader.agents.a08_execution import ExecutionAgent, marketable_exit_price
from trader.agents.a09_positions import (
    MANUAL_ROUTES,
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
from trader.agents.alerts_core import CoreAlerts, grading_report, session_open_for
from trader.agents.execution_quality import SlippageMonitor
from trader.agents.shadow import ShadowBook, shadow_report
from trader.agents.speed import LatencyTracker, PreArmCache, PreArmed, expected_setup, rebind
from trader.bus import EventBus
from trader.calendar import MacroCalendar, MarketCalendar, TradingWindows
from trader.config import Config
from trader.interfaces import Broker, BrokerRejected, BrokerUnavailable, HistoricalData, LLMClient
from trader.journal import schema as s
from trader.journal.store import JournalUnavailable, SqlJournal
from trader.models.messages import (
    AFTERNOON_SETUPS,
    MORNING_SETUPS,
    ApprovalResponse,
    Bar,
    ChecklistItem,
    Command,
    Decision,
    EventFlags,
    GamePlan,
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
from trader.notify.chart import approval_chart
from trader.notify.commands import CommandAuthority
from trader.timeutil import ET, et_date, et_datetime, to_et

log = structlog.get_logger(__name__)
CHECKLIST_FAIL_PREFIXES = ("NT_CK", "NT_MF")    # the view's own checklist (OP-9)
LLM_UNLOAD_AT = time(17, 45)   # after the daily/weekly/monthly reports
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
        self._plan_attempts: set[tuple[date, str]] = set()   # (day, window) 9:00/2:45 runs
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
        # REQUIREMENTS 1.3
        self.latency = LatencyTracker(journal, cfg.latency.budget_s)            # LAT-1, LAT-4
        self.prearm = PreArmCache(cfg.latency.prearm_atr,
                                  cfg.latency.prearm_tolerance_atr)             # LAT-2
        self.prearm_enabled = cfg.latency.prearm and not sim_mode
        self.shadow = ShadowBook(journal, cfg.entries.cancel_unfilled_after_min,
                                 cfg.entries.cancel_if_runs_atr5) if cfg.shadow.enabled \
            else None                                                           # SH-1
        self._shadowed: set[str] = set()
        self._timing: dict[str, dict[str, Any]] = {}
        self.slippage = SlippageMonitor(cfg.execution.slippage_switch_r,
                                        cfg.execution.slippage_window)          # EXQ-1
        self.regime = "normal"                                                  # RG-1
        self._macro_done: set[datetime] = set()                                 # TW-6
        self._day_cache: dict[str, Any] = {}       # per-day flatten times / TW-6 block starts
        self.account_type: str | None = None                                    # AC-2
        self.settled_cash: Decimal | None = None                                # AC-1
        self.core_alerts = CoreAlerts(                                          # AL-1..AL-7
            cfg, self.md, self.ta, self.risk, self.windows, journal,
            locked=lambda now: {x["ticker"] for x in self.compliance.locked_list(now)},
            equity=lambda: self.equity)
        self.copilot_plans: dict[str, tuple[SetupTriggered, Decision, datetime]] = {}  # CP-1
        self.options: Any = None          # OPTIONS_REQUIREMENTS (set by trader-core)
        self.bear = BearishScanner(cfg, self.md, self.ta)                      # BR-1..BR-4
        self._bear_buffer: list[BearSignal] = []
        self._reject_codes: dict[str, str] = {}     # proposal id -> reason (OP-9 view check)
        self.fallback: Any = None         # NF-1 local fallback channel (e-mail), optional
        self.snapshots: Any = None        # UN-1: async (symbols) -> Alpaca snapshots
        self.sip_history: Any = None      # DQ-6: HistoricalData on the SIP feed
        self.in_play_today: list[dict[str, Any]] = []
        self._setup_jobs()

    # ===================================================================================
    # lifecycle
    # ===================================================================================
    def _setup_jobs(self) -> None:
        sch = self.scheduler
        # until: how late a job missed during a restart may still run (default 30 min).
        # Plans and levels are in-memory state the windows need, so they catch up until the
        # window's last entry; a plan that fell back (LLM down) is retried every 5 minutes.
        sch.add("preflight", time(7, 0), self.job_preflight, until=time(9, 35))
        sch.add("levels", time(8, 0), self.job_levels, until=time(15, 50))
        if not self.sim_mode:
            sch.add("in_play", time(8, 30), self.job_in_play, until=time(9, 25))     # UN-1
            sch.add("data_divergence", time(16, 20), self.job_divergence,
                    until=time(23, 59))                                              # DQ-6
            sch.add("options_chains", time(8, 30), self.job_options_chains,
                    until=time(9, 25))                                               # A13-1
            sch.add("options_close", time(16, 5), self.job_options_close,
                    until=time(23, 59))                                              # A16-1
        sch.add("morning_plan", time(9, 0), lambda now: self.job_plan(now, "morning"),
                until=time(10, 15), retry_min=5)
        sch.add("premarket_levels", time(9, 30), self.job_open_refresh, until=time(10, 15))
        sch.add("morning_no_trade_log", time(10, 16), lambda now: self.job_window_end(
            now, "morning"))
        sch.add("morning_summary", time(10, 35), lambda now: self.job_report(now, "morning"))
        sch.add("afternoon_plan", time(14, 45), lambda now: self.job_plan(now, "afternoon"),
                until=time(15, 50), retry_min=5)
        sch.add("year_end_check", time(15, 45), self.job_year_end)
        sch.add("afternoon_no_trade_log", time(15, 51), lambda now: self.job_window_end(
            now, "afternoon"))
        sch.add("end_of_day", time(16, 0), self.job_end_of_day, until=time(23, 59))
        # A5-1 preview: next session's plan from the day just closed (daily bar is final).
        # Live only: replays and backtests keep their recorded plan schedule (RP-2).
        if not self.sim_mode:
            sch.add("next_session_plan", time(16, 15), self.job_next_plan, until=time(23, 59))
            # after the 4:30-5:30 reports: free the GPU and ~3 GB RAM overnight; QW-4 reloads
            # the model at 8:55 and the news service loads it on its first call from 7:15
            sch.add("llm_unload", LLM_UNLOAD_AT, self._unload_llm, when="daily",
                    until=time(23, 59))
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
        self._restore_slippage()
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

    def _restore_slippage(self) -> None:
        """EXQ-1: rebuild per-setup entry slippage from closed positions."""
        try:
            rows = self.journal.closed_positions()
        except Exception:   # noqa: BLE001
            return
        for r in sorted(rows, key=lambda x: x["closed_at"]):
            st = r.get("state_json") or {}
            if not st.get("trigger") or r.get("avg_entry") is None or not r.get("setup"):
                continue
            entry, trig = Decimal(str(r["avg_entry"])), Decimal(str(st["trigger"]))
            rps = entry - Decimal(str(st.get("initial_stop", entry)))
            self._apply_slippage_mode(r["setup"], self.slippage.record(
                r["setup"], SlippageMonitor.slip_r(entry, trig, rps)), announce=False)

    def _apply_slippage_mode(self, setup: str, mode: str | None, announce: bool = True) -> None:
        if mode == "passive":
            self.ta.passive_setups.add(setup)
        elif mode == "paper":
            self.ta.passive_setups.discard(setup)
            self.risk.state.slippage_paper.add(setup)
        if mode and announce:
            self.journal.incident(self.now(), "EXQ1_" + mode.upper(),
                                  f"{setup}: average entry slippage above "
                                  f"{self.cfg.execution.slippage_switch_r}R -> {mode}")
            self._spawn(self.say(f"EXQ-1: {setup} slippage too high -> {mode}", "alert"))

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
        self.bear.start_day(d)
        self._reject_codes = {}
        self._window_orders = {"morning": 0, "afternoon": 0}
        self._candidates = {}
        self._macro_done = set()
        self.prearm.drop_day(d)
        self._set_regime("normal", None, "not yet classified", self.now(), journal=False)
        await self._refresh_account()
        self.risk.start_day(d, self.equity)
        self.risk.state.kill_switch = self.sup.kill_switch
        self.risk.state.paused_styles = set(self.sup.paused)
        await self.job_levels(self.now())
        self._apply_external_locks()                                            # WS-9
        self.core_alerts.start_day(session_open_for(d), {
            sym: st.prior_close for sym, st in self.md.state.items()})
        acts = self.pm.start_day(d, self.ta.levels)
        await self.execute_actions(acts)
        self.journal.snapshot_config(d, self.now(), _cfg_hash(self.cfg), "")

    # ===================================================================================
    # scheduled jobs
    # ===================================================================================
    def _apply_external_locks(self) -> None:
        try:
            trades = ext.load(self.journal)
        except Exception:   # noqa: BLE001 - table missing on an old journal
            return
        if trades:
            n, warnings = ext.apply_locks(self.compliance, trades, self.now(),
                                          self.cfg.wash_sale.lock_days, self.cfg.canonical)
            for w in warnings:
                log.warning("external_trade_in_lock", detail=w)
            if n:
                log.info("external_locks_created", count=n)

    async def job_in_play(self, now: datetime) -> None:
        """UN-1: the 50 largest S&P 500 names ranked by gap and pre-market volume (watch
        only; they are never traded unless the owner adds them to the list)."""
        if not self.cfg.in_play.enabled or self.snapshots is None:
            return
        try:
            snaps = await self.snapshots(list(self.cfg.in_play.symbols))
        except Exception as exc:   # noqa: BLE001
            log.warning("in_play_failed", error=str(exc))
            return
        news = {t for t, nf in self.ta.news.items() if nf.flags or nf.catalyst != "none"}
        rows = ops_mod.rank_in_play(snaps, news, self.cfg.in_play.top_n,
                                    set(self.cfg.universe.tickers))
        self.in_play_today = rows
        d = et_date(now)
        with contextlib.suppress(Exception):
            self.journal.insert_many(s.in_play, [{
                "date": d, "ticker": r["ticker"], "gap_pct": r["gap_pct"],
                "pm_volume_ratio": r["pm_volume_ratio"], "news": r["news"],
                "rank": r["rank"], "on_list": r["on_list"]} for r in rows])

    async def job_divergence(self, now: datetime) -> None:
        """DQ-6: today's IEX bars against consolidated (SIP) bars, per ticker."""
        if self.sip_history is None or self.day is None:
            return
        d = self.day
        start = et_datetime(d, time(9, 30))
        end = et_datetime(d, time(16, 0))
        setups = self.journal.rows(s.setups)
        rows = []
        for t in self.cfg.universe.tickers:
            st = self.md.state.get(t)
            if st is None or not st.bars1:
                continue
            try:
                sip = await self.sip_history.minute_bars(t, start, end)
            except Exception as exc:   # noqa: BLE001
                log.warning("sip_history_failed", ticker=t, error=str(exc))
                continue
            trig = [(r["ts"], float(r["trigger"])) for r in setups
                    if r.get("ticker") == t and r.get("ts") is not None
                    and et_date(r["ts"]) == d and r.get("trigger") is not None]
            rows.append(ops_mod.divergence_row(d, t, st.bars1, sip, trig,
                                               self.cfg.data.sip_divergence_pct))
        if rows:
            self.journal.insert_many(s.data_divergence, rows)
            worst = max((r for r in rows if r["or_diff_pct"] is not None),
                        key=lambda r: r["or_diff_pct"], default=None)
            if worst is not None and worst["or_diff_pct"] > self.cfg.data.sip_divergence_pct:
                await self.say(f"DQ-6: IEX vs SIP opening range differs by "
                               f"{worst['or_diff_pct']:.3f}% on {worst['ticker']} today "
                               f"(tolerance {self.cfg.data.sip_divergence_pct}%)", "alert")

    def _pt4_text(self, d: date) -> str:
        """PT-4 summary for the daily report (paper environment only)."""
        if self.cfg.mode.environment != "paper":
            return ""
        closed = [r for r in self.journal.closed_positions()
                  if r.get("closed_at") is not None and et_date(r["closed_at"]) == d
                  and r.get("route") == "broker"]
        if not closed:
            return ""
        limits: dict[str, Decimal] = {}
        for o in self.journal.rows(s.orders):
            if o.get("leg") in ("A", "B") and o.get("limit") is not None:
                limits.setdefault(o["proposal_id"], Decimal(str(o["limit"])))
        bars = {t: st.bars1 for t, st in self.md.state.items()}
        return str(ops_mod.strict_paper(closed, limits, bars)["text"])

    async def job_preflight(self, now: datetime) -> None:
        ok, problems = self.sup.preflight(et_date(now))
        want = self.cfg.mode.account_type
        if self.account_type and want != "auto" and want != self.account_type:   # AC-2
            ok = False
            problems.append(f"{Reason.NT_ACCOUNT_MISMATCH}: config account_type {want} but "
                            f"Alpaca reports a {self.account_type} account")
        rs = self.risk.state
        text = (f"Pre-flight {to_et(now):%a %b %d} — {'GREEN' if ok else 'RED'}\n"
                + ("All checks green/amber" if ok else "NO TRADING TODAY: " + "; ".join(problems))
                + f"\nEquity ${self.equity:,.2f} (HWM ${rs.hwm:,.2f}) · risk/trade "
                f"{self.risk.risk_pct(True)}% · {self.account_type or '?'} account"
                + (f" · settled cash ${self.settled_cash:,.2f}" if self.account_type == "cash"
                   and self.settled_cash is not None else ""))
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
        self._spy_daily: list[Bar] = []
        for t in list(self.cfg.universe.tickers) + list(self.cfg.universe.context_symbols):
            try:
                daily = await self.history.daily_bars(t, d - timedelta(days=200), d)
            except Exception as exc:   # noqa: BLE001
                log.warning("levels_history_failed", ticker=t, error=str(exc))
                continue
            if t == "SPY":
                self._spy_daily = daily
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
        self._classify_regime(now, use_open=False)
        # UR-3: history check (tradability is checked against the broker in live pre-flight)
        need = self.cfg.universe.min_history_days
        for t in self.cfg.universe.tickers:
            if hist_days.get(t, 0) < need:
                self.universe.blocked[t] = f"{t}: {hist_days.get(t, 0)} days of history < " \
                                           f"{need} (UR-3)"
            else:
                self.universe.blocked.pop(t, None)

    def _classify_regime(self, now: datetime, use_open: bool) -> None:
        """RG-1 at 8:30 (pre-market price) and again at the 9:30 open."""
        spy = self.md.state.get("SPY")
        lv = self.ta.levels.get("SPY")
        gap = None
        if spy is not None and lv is not None and lv.prior_close:
            px = spy.open_price if use_open else (spy.last_price if spy.pm_bars else None)
            if px:
                gap = (px / lv.prior_close - 1) * 100
        regime, ratio, detail = classify_regime(
            getattr(self, "_spy_daily", []), et_date(now), gap,
            self.cfg.regime.high_vol_atr_ratio, self.cfg.regime.high_vol_gap_pct)
        self._set_regime(regime, ratio, detail, now, gap=gap)

    def _set_regime(self, regime: str, ratio: float | None, detail: str, now: datetime,
                    gap: float | None = None, journal: bool = True) -> None:
        changed = regime != self.regime
        self.regime = self.ta.regime = self.risk.state.regime = regime
        if journal:
            with contextlib.suppress(Exception):
                self.journal.insert(s.regime_daily, {
                    "date": et_date(now), "regime": regime, "spy_atr_ratio": ratio,
                    "spy_gap_pct": gap, "detail": detail})
        if changed and regime == "high-vol":
            self._spawn(self.say(f"Regime: HIGH-VOL ({detail}): risk per trade halved, no "
                                 "gap-and-go on outsized gaps (RG-2)", "alert"))

    async def job_open_refresh(self, now: datetime) -> None:
        """9:30: pre-market levels onto today's level map and the open-based regime."""
        self.ta.refresh_premarket()
        self._classify_regime(now, use_open=True)

    async def job_plan(self, now: datetime, window: str) -> bool:
        """A5-1 + A12-4: game plan with the locked list. Returns False when the LLM plan
        failed and the fallback (no picks) is in place, so the scheduler retries it."""
        d = et_date(now)
        if window == "afternoon" and not self.windows.afternoon_open(d):
            return True
        retry = (d, window) in self._plan_attempts   # a preview plan is not an attempt
        self._plan_attempts.add((d, window))
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
        if plan.source != "llm" and retry:
            return False   # the first fallback was already sent; say again only on success
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
        if window == "morning" and self.in_play_today:                          # UN-1
            lines.append("In play (watch only, not tradable): " + ", ".join(
                f"{r['ticker']} {r['gap_pct']:+.1f}%" for r in self.in_play_today
                if not r["on_list"]) or "-")
        lines.append(f"Regime: {self.regime}")                                   # RG-1
        left = self.cfg.risk.max_trades_per_day - rs.trades_total_today
        lines.append(f"Trades left today: {left} · Daily loss budget: ${budget:,.0f}")
        await self.say("\n".join(lines), "game_plan")
        return plan.source == "llm"   # a fallback or kept preview is retried every 5 minutes

    def _locks(self, now: datetime) -> list[dict[str, Any]]:
        locks = [{"ticker": x["ticker"],
                  "reason": f"{x['type']} until {x['until'].astimezone(ET):%Y-%m-%d}"}
                 for x in self.compliance.locked_list(now)]
        return locks + [{"ticker": t, "reason": why} for t, why in self.universe.blocked.items()]

    def next_session_day(self, now: datetime) -> date | None:
        """The session a preview plan is for: today before the open, else the next trading
        day after the close. None while the market is open."""
        d = et_date(now)
        if self.cal.is_trading_day(d):
            sess = self.cal.session(d)
            if to_et(now) < sess.open:
                return d
            if to_et(now) < sess.close:
                return None
        return self.cal.next_trading_day(d)

    async def job_next_plan(self, now: datetime) -> bool:
        """A5-1 preview (4:15 PM, or the PLAN command outside market hours): next-day ideas
        from the latest completed session. Code screens every ticker (session_screen), Qwen
        reviews only the tickers with a candidate for each window, open slots are filled from
        the screen. Journals ``next_day_ideas`` (Grafana), grades the previous ideas on the
        session just closed, sends the owner alert. The 9:00 and 2:45 plans refresh it."""
        nd = self.next_session_day(now)
        if nd is None or self.history is None:
            return True
        tickers = list(self.cfg.universe.tickers)
        screens: dict[str, dict[str, Any]] = {}
        market: dict[str, dict[str, Any]] = {}
        last_bar: dict[str, Bar] = {}
        compact: dict[str, dict[str, Any]] = {}
        news: dict[str, dict[str, Any] | None] = {}
        for t in tickers + list(self.cfg.universe.context_symbols):
            try:
                daily = await self.history.daily_bars(t, nd - timedelta(days=120), nd)
            except Exception as exc:   # noqa: BLE001
                log.warning("preview_history_failed", ticker=t, error=str(exc))
                continue
            lv = compute_levels(t, nd, daily)
            if lv is None:
                continue
            scr = session_screen(daily, lv)
            if t not in tickers:
                market[t] = {k: scr[k] for k in ("close", "change_pct", "range_pos",
                                                 "trend_up")}
                continue
            screens[t], last_bar[t] = scr, [b for b in daily if et_date(b.ts) < nd][-1]
            nf = self.ta.news.get(t)
            news[t] = {"summary": nf.summary[:300], "catalyst": nf.catalyst[:150],
                       "tone": nf.tone, "flags": list(nf.flags),
                       "earnings_date": nf.earnings_date.isoformat()
                       if nf.earnings_date else None} if nf else None
            # LLM payload: only what the plan needs (smaller prompt, faster on the 8B model)
            compact[t] = {"session": {k: v for k, v in scr.items() if k != "session_date"},
                          "levels": {k: getattr(lv, k) for k in (
                              "prior_high", "prior_low", "ema20d", "ema50d", "atr14d",
                              "high20d", "close_high20d")},
                          "news": news[t]}
        if not screens:
            log.warning("preview_no_data", plan_for=nd.isoformat())
            return False
        session_date = max(date.fromisoformat(x["session_date"]) for x in screens.values())
        last_result = self._grade_ideas(session_date, last_bar)
        locks = self._locks(now)
        plans: dict[str, GamePlan] = {}
        for window in ("morning", "afternoon"):
            if window == "afternoon" and not self.windows.afternoon_open(nd):
                continue
            allowed = MORNING_SETUPS if window == "morning" else AFTERNOON_SETUPS
            wt = {}
            for t, v in compact.items():
                cands = [c for c in v["session"]["candidates"] if c["setup"] in allowed]
                if cands:
                    wt[t] = {**v, "session": {**v["session"], "candidates": cands}}
            wdata = {"plan_for": nd.isoformat(), "market": market, "tickers": wt}
            plans[window] = await self.strategy.game_plan(nd, window, wdata, locks,
                                                          preview=True)
        # hard exclusions vs Qwen's vetoes; a ticker picked in one window is not "passed"
        off = {x["ticker"]: x["reason"] for x in locks}
        picked = {i.ticker for p in plans.values() for i in p.watch}
        passed = {t: why for p in plans.values() for t, why in p.off_limits
                  if t not in off and t not in picked}
        rows = idea_rows(now, nd, screens, news, plans, off, passed)
        try:
            self.journal.insert_many(s.next_day_ideas, rows)
        except Exception as exc:   # noqa: BLE001 - the alert still goes out
            log.warning("ideas_journal_failed", error=str(exc))
        text = alert_text(nd, session_date, market, plans, rows, off, last_result, passed)
        if self.options is not None:                                          # OP-10 (3), BR-3
            try:
                text += await self.options.next_day_section(
                    idea_views(plans, screens, compact, off))
            except Exception as exc:   # noqa: BLE001 - the stock ideas still go out
                log.warning("options_ideas_failed", error=str(exc))
        await self.say(text, "next_day_ideas")
        if to_et(now).time() >= LLM_UNLOAD_AT or not self.cal.is_trading_day(et_date(now)):
            await self._unload_llm(now)   # evening/weekend run: free VRAM and RAM again
        return True

    def _grade_ideas(self, session_date: date, bars: dict[str, Bar]) -> str | None:
        """Grade the latest ideas run for ``session_date`` on that session's daily bars."""
        try:
            rows = self.journal.rows(s.next_day_ideas, {"plan_for": session_date})
        except Exception as exc:   # noqa: BLE001
            log.warning("ideas_grade_failed", error=str(exc))
            return None
        if not rows:
            return None
        latest = max(r["run_at"] for r in rows)
        graded = []
        for r in rows:
            b = bars.get(r["ticker"])
            if r["run_at"] != latest or b is None or et_date(b.ts) != session_date:
                continue
            g = r if r.get("outcome") else {**r, **grade(r, b)}
            if not r.get("outcome"):
                self.journal.update(s.next_day_ideas, {"id": r["id"]}, grade(r, b))
            graded.append(g)
        return grade_summary(graded)

    async def _unload_llm(self, now: datetime) -> None:
        unload = getattr(self.llm, "unload", None)
        if callable(unload):
            ok = await unload()
            log.info("llm_unloaded", ok=ok, at=to_et(now).isoformat())

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
            text = rep["text"]
            if kind == "daily":
                text += "\n" + self.latency.summary() + "\n" + self.slippage.summary()
                pt4 = self._pt4_text(et_date(now))                               # PT-4
                if pt4:
                    text += "\n" + pt4
                if self.options is not None:                                     # A16-3
                    otext = self.options.report_text(since=et_datetime(et_date(now),
                                                                       time(0, 0)))
                    if otext:
                        text += "\n" + otext
            await self.say(text, f"report_{kind}")
        except JournalUnavailable as exc:
            log.error("report_failed", error=str(exc))

    async def job_weekly(self, now: datetime) -> None:
        rep = await self.reporter.weekly(et_date(now))
        text = rep["text"]
        try:   # SH-2: shadow trades of the last 7 days
            since = now - timedelta(days=7)
            rows = [r for r in self.journal.rows(s.shadow_trades, order_by="id")
                    if r.get("ts") is not None and r["ts"] >= since]
            stext, flags = shadow_report(rows, self.cfg.shadow.flag_after)
            text += "\n" + stext
            for f in flags:
                text += f"\n  FLAG: {f}"
        except Exception as exc:   # noqa: BLE001
            log.warning("shadow_report_failed", error=str(exc))
        try:   # UN-2: names in play on 3 of the last 5 sessions (only the owner edits the list)
            d = et_date(now)
            look, x = [], d
            for _ in range(self.cfg.in_play.rotation_lookback):
                look.append(x)
                x = self.cal.prev_trading_day(x)
            cands = ops_mod.rotation_candidates(self.journal.rows(s.in_play),
                                                set(self.cfg.universe.tickers),
                                                self.cfg.in_play.rotation_days, look)
            if cands:
                text += ("\nRotation candidates (UN-2, your decision; edit universe.tickers, "
                         "applies next Monday): " + ", ".join(cands))
        except Exception as exc:   # noqa: BLE001
            log.warning("rotation_report_failed", error=str(exc))
        try:   # AL-7
            since = now - timedelta(days=7)
            arows = [r for r in self.journal.rows(s.alerts_log, order_by="id")
                     if r.get("ts") is not None and r["ts"] >= since]
            atext, aflags = grading_report(arows, self.cfg.alerts.min_graded,
                                           self.cfg.alerts.min_hit_rate)
            text += "\n" + atext + "".join(f"\n  FLAG: {f}" for f in aflags)
        except Exception as exc:   # noqa: BLE001
            log.warning("alert_report_failed", error=str(exc))
        if self.options is not None:                                          # A16-3, A16-4
            try:
                otext = self.options.report_text(since=now - timedelta(days=7))
                text += ("\n" + otext if otext else "") + "\n" + self.options.promotion_text()
            except Exception as exc:   # noqa: BLE001
                log.warning("options_report_failed", error=str(exc))
        await self.say(text, "report_weekly")

    async def job_monthly(self, now: datetime) -> None:
        d = et_date(now)
        rep = await self.reporter.monthly(d.year, d.month)
        text = rep["text"]
        try:
            text += "\n" + ops_mod.tax_set_aside(self.journal.closed_positions(), d.year,
                                                 self.cfg.tax.set_aside_rate_pct)  # TX-1
            for line in ops_mod.ops_overdue(self.journal, now):                  # OPS-1/2
                text += f"\n  DUE: {line}"
        except Exception as exc:   # noqa: BLE001
            log.warning("monthly_extras_failed", error=str(exc))
        await self.say(text, "report_monthly")

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
        if self.shadow is not None:
            self.shadow.expire_all()
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
        st_ = self.md.state.get(bar.symbol)
        shock_acts, shock_alerts = self.pm.shock(bar, st_.atr5.value if st_ else None)   # NW-2
        actions += shock_acts
        for text in shock_alerts:
            self.journal.incident(now, "NEWS_SHOCK_PRICE", text)
            await self.say(text, "alert")
        for text, act in self.pm.manual_checks(bar):                          # CP-2
            await self.say(text, "copilot")
            if act is not None:
                actions.append(act)
        for text in self.core_alerts.on_bar(bar, now):                        # AL-1..AL-6
            await self.say(text, "alert_signal")
        signals, self.core_alerts.signals = self.core_alerts.signals, []
        if self.options is not None:                                          # OP-10 (2)
            for sym, direction, plan, kind, passed in signals:
                self._spawn(self.options.on_alert(sym, direction, plan, kind, passed))
        if self.shadow is not None and self.day is not None:                  # SH-1
            self.shadow.on_bar(bar, self._cached("flatten", lambda d: {
                w: self.windows.flatten_time(d, w) for w in ("morning", "afternoon")}))
        setups: list[SetupTriggered] = []
        if bar.symbol in self.cfg.universe.tickers:
            setups += self.ta.on_1m_close(bar, now)
        for b5 in closed5:
            actions += self._on_5m(b5, now)
            if b5.symbol in self.cfg.universe.tickers:
                setups += self.ta.on_5m_close(b5, now)
                self._bear_buffer += self.bear.on_5m_close(b5, now)          # BR-1
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
            if any(p.ticker == st.symbol for p in self.pm.open_positions()):   # NW-2
                self._news_shock(self.cfg.canonical(st.symbol), f"trading halt {st.detail}")

    def on_news(self, flags: EventFlags) -> None:
        t = self.cfg.canonical(flags.ticker)
        prev = self.ta.news.get(t)
        self.ta.news[t] = flags
        self.compliance.news[t] = flags
        self.pm.news[t] = flags
        # NW-2: a new negative 8-K or rating change on a ticker we hold -> alert, no new
        # entries in that ticker for the rest of the day
        new = set(flags.flags) - set(prev.flags if prev else ())
        held = any(self.cfg.canonical(p.ticker) == t for p in self.pm.open_positions())
        if held and flags.tone == "negative" and new & {"NEW_8K", "RATING_CHANGE"}:
            self._news_shock(t, f"negative {'/'.join(sorted(new & {'NEW_8K', 'RATING_CHANGE'}))}"
                                f": {flags.summary[:160]}")

    def _news_shock(self, ticker: str, why: str) -> None:
        if ticker in self.risk.state.news_blocked:
            return
        self.risk.state.news_blocked.add(ticker)
        now = self.now()
        self.journal.incident(now, "NEWS_SHOCK", f"{ticker}: {why}")
        self._spawn(self.say(f"NEWS SHOCK {ticker}: {why}. No new entries in {ticker} today "
                             "(NW-2); review the open position.", "alert"))

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
                self._bear_buffer += self.bear.on_5m_close(b5, now)          # BR-1
        await self.execute_actions(self.pm.on_tick(now))
        await self._macro_breakeven(now, d)
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
        digest = self.core_alerts.flush(now)                                  # AL-6
        if digest:
            await self.say(digest, "alert_digest")
        if now.second < 2 or self.sim_mode:
            self.core_alerts.grade(now)                                       # AL-7
        level, detail = self.latency.health()                                 # LAT-1
        self.sup.set_health("latency", level, detail, now)  # type: ignore[arg-type]
        if self.prearm_enabled:
            self._prearm_scan(now, d)
        if self._bear_buffer:
            bears, self._bear_buffer = self._bear_buffer, []
            await self._on_bears(bears)
        if self.options is not None and (now.second < 2 or self.sim_mode) \
                and self.windows.window_at(now) is not None:
            if self.sim_mode:                                                 # A16-1
                await self.options.on_minute(now)
            else:
                self._spawn(self.options.on_minute(now))
        if self._buffer:
            buf, self._buffer = sorted(self._buffer, key=lambda x: -x.score), []
            if self.sim_mode:   # replay: inline, deterministic
                for setup in buf:
                    await self.evaluate_setup(setup)
                    await self._options_view(setup)
            else:   # live: Qwen calls must never delay flatten times, exits or cancels
                self._spawn(self._evaluate_batch(buf))

    async def _macro_breakeven(self, now: datetime, d: date) -> None:
        """TW-6: once per block, just before it starts, protect open day trades at +0.5R."""
        for start in self._cached("macro_starts", self.windows.macro_block_starts):
            if start in self._macro_done or not (start <= now < start + timedelta(minutes=8)):
                continue
            self._macro_done.add(start)
            last = {t: st.last_price for t, st in self.md.state.items()
                    if st.last_price is not None}
            acts = self.pm.macro_breakeven(last, self.cfg.windows.macro_days.breakeven_at_r)
            await self.execute_actions(acts)
            if acts:
                await self.say(f"TW-6: {len(acts)} day trade(s) moved to break-even before "
                               f"the {to_et(start):%H:%M} block", "trade")

    def _cached(self, name: str, fn: Callable[[date], Any]) -> Any:
        """Calendar-derived values are computed once per day (exchange_calendars is slow)."""
        d = self.day
        if self._day_cache.get("_day") != d:
            self._day_cache = {"_day": d}
        if name not in self._day_cache:
            self._day_cache[name] = fn(d) if d is not None else {}
        return self._day_cache[name]

    def _prearm_scan(self, now: datetime, d: date) -> None:
        """LAT-2: when price is within 0.3 ATR(5m) of a game-plan trigger, write and review
        the proposal before the candle closes."""
        window = self.windows.window_at(now)
        plan = self.strategy.plans.get((d, window)) if window else None
        if plan is None:
            return
        for item in plan.watch:
            st = self.md.state.get(item.ticker)
            if st is None:
                continue
            k = self.prearm.key(d, window or "", item.ticker, item.setup)
            if self.prearm.should_arm(k) and self.prearm.near(item, st.last_price,
                                                               st.atr5.value):
                self.prearm.in_flight.add(k)
                self._spawn(self._prearm(item, window or "", k, now))
                if self.options is not None:                                  # OP-20
                    self._spawn(self.options.prefetch(item.ticker))

    async def _prearm(self, item: Any, window: str, k: tuple[date, str, str, str],
                      now: datetime) -> None:
        try:
            st = self.md.state[item.ticker]
            atr5 = st.atr5.value or 0.0
            entry = self.ta.entry_price(item.trigger, atr5, item.setup)
            exp = expected_setup(item, window, atr5, entry, now,
                                 f"prearm-{k[0]}-{window}-{item.ticker}-{k[3]}")
            ctx = self.context(exp, now, True)
            data = self._llm_data(exp, ctx, True)
            t0 = asyncio.get_running_loop().time()
            out = await self.strategy.propose(exp, data, True, float(st.last_price or 0))
            review = None
            if out.proposal is not None and out.proposal.action == "take":
                review, _err = await self.reviewer.review(out.proposal, data)
            self.prearm.entries[k] = PreArmed(item, exp, out.proposal, review,
                                              asyncio.get_running_loop().time() - t0, now)
            log.info("prearmed", ticker=item.ticker, setup=item.setup,
                     ok=review is not None)
        except Exception as exc:   # noqa: BLE001 - pre-arming is an optimisation only
            log.warning("prearm_failed", ticker=item.ticker, error=str(exc))
        finally:
            self.prearm.in_flight.discard(k)

    async def _evaluate_batch(self, buf: list[SetupTriggered]) -> None:
        async with self._eval_lock:   # one pipeline at a time: limits see every prior decision
            for setup in buf:
                try:
                    await self.evaluate_setup(setup)
                    await self._options_view(setup)
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
        self.account_type = acct.account_type                                   # AC-2
        want = self.cfg.mode.account_type
        if want == "cash" or (want == "auto" and acct.account_type == "cash"):  # AC-1
            self.settled_cash = max(D0, acct.cash - self._unsettled_proceeds())
            self.buying_power = min(self.buying_power, self.settled_cash)
        else:
            self.settled_cash = None

    def _unsettled_proceeds(self) -> Decimal:
        """AC-1: proceeds of today's sells settle the next business day (T+1)."""
        if self.day is None:
            return D0
        start = et_datetime(self.day, time(0, 0))
        try:
            rows = self.journal.rows(s.fills, {"side": "sell"})
        except Exception:   # noqa: BLE001
            return D0
        return sum((Decimal(str(r["price"])) * int(r["qty"]) for r in rows
                    if r.get("ts") is not None and r["ts"] >= start), D0)

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
        self._reject_codes[pid] = code
        self._missed(s_, pid, code)

    def _missed(self, s_: SetupTriggered, pid: str, code: str) -> None:
        """LAT-4 + SH-1: every trigger that does not become an order is logged with its
        reason and shadow-traded."""
        t = self._timing.pop(pid, {})
        self.latency.missed_trade(s_, pid, self.now(), self.cfg.style_mode(s_.style), code,
                                  t.get("model_s"), t.get("owner_s"), t.get("prearmed", False))
        self._add_shadow(s_, pid, False, code)

    def _add_shadow(self, s_: SetupTriggered, pid: str, taken: bool, reason: str) -> None:
        if self.shadow is None or s_.setup_id in self._shadowed:
            return
        self._shadowed.add(s_.setup_id)
        self.shadow.add(s_, pid, taken, reason)

    async def _on_bears(self, bears: list[BearSignal]) -> None:
        """BR-4: journal each bearish signal like a long setup; options only."""
        for sig in bears:
            try:
                self.journal.upsert(s.setups, {"id": sig.setup_id}, sig.journal_row())
            except Exception as exc:   # noqa: BLE001
                log.warning("bear_journal_failed", error=str(exc))
            if self.options is None:
                continue
            if self.sim_mode:
                await self.options.on_bear(sig)
            else:
                self._spawn(self.options.on_bear(sig))

    async def _options_view(self, s_: SetupTriggered) -> None:
        """OP-10 (1): after the stock pipeline decided. OP-9: no suggestion when the view's
        own checklist failed; A15-2: none when the stock entry was taken."""
        if self.options is None:
            return
        pid = proposal_id_for(s_.setup_id)
        code = self._reject_codes.get(pid, "")
        checklist_ok = not code.startswith(CHECKLIST_FAIL_PREFIXES) and code not in (
            Reason.NT_UNIVERSE, Reason.NT_NO_RELIABLE_QUOTE, Reason.NT_OPTIONS_ONLY)
        plan = self.copilot_plans.get(self.cfg.canonical(s_.ticker))
        stock_taken = self.exec.seen(pid) or pid in self._pending_approval or (
            plan is not None and plan[0].setup_id == s_.setup_id)
        try:
            await self.options.on_setup(s_, checklist_ok, stock_taken)
        except Exception as exc:   # noqa: BLE001 - options never affect the stock pipeline
            log.warning("options_view_failed", ticker=s_.ticker, error=str(exc))

    async def job_options_chains(self, now: datetime) -> None:
        if self.options is not None:
            await self.options.job_chain_load(now)

    async def job_options_close(self, now: datetime) -> None:
        if self.options is not None:
            await self.options.job_close(now)

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
        if s_.context.get("direction") == "bear":                             # BR-4, ADR-008
            self._reject(s_, pid, Reason.NT_OPTIONS_ONLY, "bearish signal: options only")
            return None
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
        # A5 proposal (Qwen) - or the pre-armed one (LAT-2, A5-5)
        data = self._llm_data(s_, ctx, planned)
        pa = self.prearm.match(s_) if self.prearm_enabled else None
        if pa is not None:
            prop_, review_ = rebind(pa, s_, pid, now, self.strategy.expiry(s_, now))
            self._timing[pid] = {"model_s": 0.0, "prearmed": True}
            self.journal.upsert(s.proposals, {"proposal_id": pid}, {
                "setup_id": s_.setup_id, "ts": now, "ticker": s_.ticker,
                "json": {**prop_.model_dump(mode="json"), "prearmed": True}, "valid": True,
                "error": None, "model": prop_.model, "prompt_version": prop_.prompt_version,
                "latency_ms": 0, "tokens": 0})
            self.journal.upsert(s.reviews, {"proposal_id": pid}, {
                "ts": now, "verdict": review_.verdict, "main_risk": review_.main_risk,
                "rules_broken": list(review_.rules_broken),
                "json": {**review_.model_dump(mode="json"), "prearmed": True}})
            self.stats.proposals += 1
            self.bus.publish(prop_)
            self.bus.publish(review_)
            return await self._after_review(s_, pid, prop_, review_, planned, now)
        t0 = asyncio.get_running_loop().time()
        out = await self.strategy.propose(s_, data, planned, float(ctx.last_price))
        self._timing[pid] = {"model_s": asyncio.get_running_loop().time() - t0,
                             "prearmed": False}
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
        self._timing[pid]["model_s"] = asyncio.get_running_loop().time() - t0
        self.stats.reviews += 1
        if review is None:
            self._reject(s_, pid, Reason.NT_LLM_INVALID, f"A6: {err} (QW-3: no trade "
                         "without a review)", pre)
            return None
        self.bus.publish(review)
        return await self._after_review(s_, pid, prop, review, planned, now)

    async def _after_review(self, s_: SetupTriggered, pid: str, prop: TradeProposal,
                            review: Any, planned: bool, now: datetime) -> Decision | None:
        # A12 + A7 on fresh live data
        ctx = self.context(s_, self.now(), planned)
        comp = self.compliance.check_entry(s_.ticker, self.now(), s_.style, pid)
        self.bus.publish(comp)
        dec = self.risk.evaluate(s_, ctx, review, comp, pid)
        self._journal_decision(dec, s_)
        if dec.result == "REJECT":
            self._note_candidate(s_, list(dec.checklist), dec.reason_code)
            self.stats.rejected += 1
            self._missed(s_, pid, dec.reason_code)
            return dec
        if dec.reason_code == Reason.COPILOT_PLAN:                              # CP-1
            await self._copilot_plan(s_, dec, prop)
            return dec
        if dec.result == "NEEDS_APPROVAL":
            self._pending_approval.add(pid)
            self._spawn(self._approval_flow(s_, prop, dec, review.verdict, planned))
            return dec
        await self._submit(dec, s_, planned)
        return dec

    async def _copilot_plan(self, s_: SetupTriggered, dec: Decision, prop: TradeProposal
                            ) -> None:
        """CP-1: send the full plan at the trigger; the owner executes, no order here."""
        now = self.now()
        self.copilot_plans[self.cfg.canonical(s_.ticker)] = (s_, dec, now)
        st = self.md.state.get(s_.ticker)
        rr = ""
        if s_.resistance is not None and dec.entry > dec.stop:
            room = (Decimal(str(s_.resistance)) - dec.entry) / (dec.entry - dec.stop)
            rr = f" · R:R to resistance {room:.1f}"
        await self.say(
            f"COPILOT PLAN {s_.ticker} {s_.setup} ({s_.window}): buy {dec.qty} @ "
            f"<={dec.entry} "
            f"· stop {dec.stop} · T1 {dec.t1} (sell {dec.qty // 2}) · T2 {dec.t2} · risk "
            f"${dec.risk_usd:.0f} ({dec.risk_pct}%){rr} · RVOL "
            f"{(st.rvol() or 0) if st else 0:.1f}\nThesis: {prop.thesis}\nNo order placed. "
            f"After you buy: FILLED {s_.ticker} <qty> @ <price> (Alpaca fills are picked up "
            "automatically).", "copilot")
        t = self._timing.pop(dec.proposal_id, {})
        self.latency.order(s_, dec.proposal_id, now, "copilot", t.get("model_s"), None,
                           t.get("prearmed", False))
        self._add_shadow(s_, dec.proposal_id, True, "COPILOT")

    async def manual_fill(self, ticker: str, qty: int, price: Decimal, ts: datetime,
                          route: str) -> str:
        """CP-2: the owner bought (Alpaca fill or FILLED command): track it and send the
        exits as alerts. Counts toward every limit and statistic (tagged manual, DR-8)."""
        t = self.cfg.canonical(ticker)
        if any(p.ticker == t and p.route in MANUAL_ROUTES for p in self.pm.open_positions()):
            return f"{t}: a manual position is already tracked"
        plan = self.copilot_plans.get(t)
        st = self.md.state.get(t)
        atr_v = (st.atr5.value if st and st.atr5.value else 0) or float(price) * 0.003
        atr5 = Decimal(str(atr_v))
        if plan is not None:
            s_, dec, _ = plan
            stop, t1, t2, setup, window = dec.stop, dec.t1, dec.t2, s_.setup, s_.window
            trigger = s_.trigger
        else:   # no plan: 1 ATR(5m) stop, 1R/2R targets
            stop = (price - atr5).quantize(Decimal("0.01"))
            r = price - stop
            t1, t2 = price + r, price + 2 * r
            setup, window = "MANUAL", self.windows.window_at(ts) or "morning"
            trigger = price
        pid = f"manual-{t}-{ts:%Y%m%d%H%M%S}"
        p = self.pm.open_manual(pid, t, route, qty, price, ts, setup=setup, window=window,
                                stop=stop, t1=t1, t2=t2, atr5=atr5, trigger=trigger)
        self.journal.insert(s.positions, {
            "position_id": pid, "ticker": t, "style": "day", "window": window, "setup": setup,
            "planned": plan is not None, "opened_at": ts, "qty": qty, "avg_entry": price,
            "initial_stop": stop, "risk_usd": (price - stop) * qty, "route": route,
            "override": True, "state_json": self._pos_state(p)})
        st_risk = self.risk.state
        st_risk.trades_today[window] = st_risk.trades_today.get(window, 0) + 1
        st_risk.window_entries.add((window, t))
        self.compliance.on_buy_fill(pid, t, qty, price, ts, "broker")
        return (f"COPILOT tracking {t} {qty} @ {price}: stop {stop} · T1 {t1} · T2 {t2}. "
                "Exit instructions follow as alerts.")

    async def manual_exit(self, ticker: str, qty: int, price: Decimal, ts: datetime) -> str:
        """CP-2: the owner sold part or all of a tracked manual position."""
        t = self.cfg.canonical(ticker)
        p = next((x for x in self.pm.open_positions() if x.ticker == t and x.route in
                  MANUAL_ROUTES), None)
        if p is None:
            return f"{t}: no tracked manual position"
        q = min(qty, p.qty_open)
        p.exit_qty += q
        p.exit_value += price * q
        self.compliance.on_sell_fill(p.position_id, t, q, price, ts,
                                     p.r_multiple() if p.closed else 0.0)
        if p.closed:
            p.exit_reason = p.exit_reason or p.pending_exit or Reason.EXIT_FLATTEN_CMD
            await self._finalize(p, ts)
            return f"COPILOT {t} closed"
        self._persist(p)
        return f"COPILOT {t}: {q} sold, {p.qty_open} still open"

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
            sent_at = self.now()
            expires = sent_at + timedelta(seconds=timeout)   # the code lives as long as the ask
            # LAT-4: the proposal outlives the approval window by the grace period, so an
            # in-time YES is never refused as expired
            valid_until = max(prop.expires_at, expires + timedelta(
                seconds=self.cfg.latency.approval_grace_s))
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
            attach = getattr(self.notifier, "attach_chart", None)
            if callable(attach) and self.cfg.notifications.approval_chart:      # AP-1
                png = self._approval_png(s_, dec)
                if png:
                    attach(pid, base64.b64encode(png).decode())
            updater = asyncio.ensure_future(self._approval_updates(s_, pid, fut, expires))
            try:
                resp = await self.notifier.request_approval(
                    pid, self.sup.mode_tag() + " " + text, code, timeout)
            finally:
                updater.cancel()
            answer: str = resp.answer
            if answer == "timeout" and fut.done():
                answer = fut.result()
            self.journal.update(s.approvals, {"proposal_id": pid},
                                {"answered_at": self.now(), "answer": answer})
            self._timing.setdefault(pid, {})["owner_s"] = (self.now() - sent_at
                                                           ).total_seconds()
            self.stats.approvals += 1
            if answer != "yes":
                code_ = Reason.NT_APPROVAL_REJECTED if answer == "no" else \
                    Reason.NT_APPROVAL_TIMEOUT
                self._reject(s_, pid, code_, f"owner answer: {answer}")
                return
            if self.now() > valid_until:
                self._reject(s_, pid, Reason.NT_EXPIRED, "proposal expired (A5-4, LAT-4)")
                return
            # re-run the full checklist on live data right before submitting
            ctx = self.context(s_, self.now(), planned)
            comp = self.compliance.check_entry(s_.ticker, self.now(), s_.style, pid)
            from trader.models.messages import Review as _Review
            review = _Review(proposal_id=pid, verdict="agree", main_risk="owner approved")
            final = self.risk.evaluate(s_, ctx, review, comp, pid)
            if final.result == "REJECT":
                self._journal_decision(final, s_)
                self._missed(s_, pid, final.reason_code)
                return
            final = final.model_copy(update={"result": "APPROVE", "reason_code": Reason.APPROVED,
                                             "override": dec.override or verdict != "agree"})
            self._journal_decision(final, s_)
            await self._submit(final, s_, planned)
        finally:
            self._pending_approval.discard(pid)
            self._approval_waiters.pop(pid, None)

    def _approval_png(self, s_: SetupTriggered, dec: Decision) -> bytes | None:
        st = self.md.state.get(s_.ticker)
        if st is None or not st.bars5:
            return None
        try:
            return approval_chart(st.bars5, st.vwap_at_5m, entry=float(dec.entry),
                                  stop=float(dec.stop), t1=float(dec.t1),
                                  or_high=st.or5[0] if st.or5 else None,
                                  or_low=st.or5[1] if st.or5 else None)
        except Exception as exc:   # noqa: BLE001 - a chart never blocks an approval
            log.warning("approval_chart_failed", error=str(exc))
            return None

    def approval_status(self, s_: SetupTriggered, expires: datetime, now: datetime) -> str:
        """AP-3: live price, distance from the trigger in ATR(5m), seconds left, chase."""
        st = self.md.state.get(s_.ticker)
        last = st.last_price if st else None
        left = max(0, int((expires - now).total_seconds()))
        if last is None or s_.atr5 <= 0:
            return f"⏱ {left}s left"
        dist = (last - float(s_.trigger)) / s_.atr5
        chase = self._chase_at_send(s_, now)
        return (f"⏱ {left}s left · live {last:.2f} · {dist:+.2f} ATR from trigger "
                f"{s_.trigger}" + (" · WOULD NOW BE A CHASE: will be refused" if chase else ""))

    async def _approval_updates(self, s_: SetupTriggered, pid: str, fut: asyncio.Future[str],
                                expires: datetime) -> None:
        upd = getattr(self.notifier, "update_approval", None)
        if not callable(upd):
            return
        every = self.cfg.notifications.approval_live_update_s
        with contextlib.suppress(asyncio.CancelledError):
            while not fut.done() and self.now() < expires:
                await asyncio.sleep(every)
                if fut.done():
                    break
                await upd(pid, self.approval_status(s_, expires, self.now()))

    async def _submit(self, dec: Decision, s_: SetupTriggered, planned: bool) -> None:
        now = self.now()
        if self.risk.state.kill_switch:
            self._reject(s_, dec.proposal_id, Reason.NT_KILL_SWITCH, "kill switch on at submit")
            return
        chase = self._chase_at_send(s_, now)                                    # LAT-3
        if chase:
            self._reject(s_, dec.proposal_id, Reason.NT_CHASE_AT_SEND, chase)
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
        t = self._timing.pop(dec.proposal_id, {})
        self.latency.order(s_, dec.proposal_id, self.now(), self.cfg.style_mode(s_.style),
                           t.get("model_s"), t.get("owner_s"), t.get("prearmed", False))
        self._add_shadow(s_, dec.proposal_id, True, "TAKEN")
        self._sim_route_children(ticket.proposal_id)
        await self.say(f"SUBMITTED {s_.ticker} {s_.setup} {dec.qty} @ ≤{dec.entry} stop {dec.stop}"
                       f" T1 {dec.t1} T2 {dec.t2}" + (" [paper]" if dec.route == "paper" else ""),
                       "trade")

    def _chase_at_send(self, s_: SetupTriggered, now: datetime) -> str:
        """LAT-3: CK-6 again at send time with the live quote (the ask when it is reliable,
        else the last trade). Returns the refusal detail, or "" when the order may go."""
        st = self.md.state.get(s_.ticker)
        if st is None:
            return ""
        live: float | None = None
        if st.quote is not None and self.ta.quote_reliable(s_.ticker, now):
            live = st.quote.ask
        elif st.last_price is not None:
            live = st.last_price
        if live is None or s_.atr5 <= 0:
            return ""
        # the same CK-6 rule as at proposal time: the entry limit is capped at trigger +
        # 0.25 ATR5 (fixed), and the live price must not have run past trigger + 0.5 ATR5
        runaway = float(s_.trigger) + self.cfg.entries.cancel_if_runs_atr5 * s_.atr5
        if live >= runaway:
            return (f"CK-6 at send: live {live:.2f} >= trigger {s_.trigger} + "
                    f"{self.cfg.entries.cancel_if_runs_atr5} ATR5 ({runaway:.2f})")
        return ""

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
        if (u.event in ("fill", "partial_fill") and u.fill_qty > 0 and u.fill_price is not None
                and o.id not in self.exec.order_index and o.id not in self.pm.by_order
                and o.id not in self._exit_orders and o.parent_id is None):
            # CP-2: an order the owner placed in the Alpaca app (not ours)
            if o.side == "buy" and self.cfg.style_mode("day") == "copilot":
                await self.say(await self.manual_fill(o.symbol, u.fill_qty, u.fill_price, u.ts,
                                                      "manual"), "copilot")
                return
            if o.side == "sell" and any(p.ticker == self.cfg.canonical(o.symbol)
                                        and p.route in MANUAL_ROUTES
                                        for p in self.pm.open_positions()):
                await self.say(await self.manual_exit(o.symbol, u.fill_qty, u.fill_price,
                                                      u.ts), "copilot")
                return
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
        if p.route == "broker" or p.route in MANUAL_ROUTES:   # CP-2: manual counts too
            hits = self.risk.on_trade_closed(ClosedTrade(
                position_id=p.position_id, ticker=p.ticker, setup=p.setup, style=p.style,
                window=p.window, closed_at=ts, pnl=pnl, r_multiple=r, exit_reason=reason,
                route=p.route, reduced_size=p.reduced_size), ts)
        else:
            self.risk.state.history.append(ClosedTrade(
                position_id=p.position_id, ticker=p.ticker, setup=p.setup, style=p.style,
                window=p.window, closed_at=ts, pnl=pnl, r_multiple=r, exit_reason=reason,
                route=p.route))
        if p.route in ("broker", "paper") and p.setup != "MANUAL":               # EXQ-1
            self._apply_slippage_mode(p.setup, self.slippage.record(
                p.setup, SlippageMonitor.slip_r(p.avg_entry, p.trigger, p.r_per_share)))
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
        if p.route in MANUAL_ROUTES:                                            # CP-2
            await self.say(f"COPILOT {p.ticker}: move your stop {old} -> {mv.new_stop} "
                           f"({mv.reason})", "copilot")
            self._persist(p)
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
        if p.route in MANUAL_ROUTES:                                            # CP-2
            await self.say(f"COPILOT EXIT {p.ticker}: sell {p.qty_open} now ({a.reason}). "
                           f"Then: SOLD {p.ticker} {p.qty_open} @ <price>", "copilot")
            return
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
        elif action == "ALERTS":                                                # AL-4
            self.core_alerts.muted = arg.strip() == "OFF"
            reply = f"Signal alerts {'off' if self.core_alerts.muted else 'on'}"
        elif action in ("FILLED", "SOLD"):                                      # CP-2
            reply = await self._owner_fill_command(action, arg)
        elif action in ("OPT", "CHAIN", "IV", "OPTS", "OPTIONS"):                # OP-13
            reply = (await self.options.command(action, arg) if self.options is not None
                     else "options module is not running (options.mode off)")
        elif action == "RECAP":                                                 # UI-34
            days = int(arg) if arg.strip().isdigit() and 1 <= int(arg) <= 250 else 5
            if self.history is None:
                reply = "RECAP needs the market-data history client"
            else:
                async def _recap() -> None:
                    from trader.agents.recap import run_recap

                    try:
                        rows, text = await run_recap(self.cfg, self.journal, self.history,
                                                     self.cal, self.macro, days, self.now())
                    except Exception as exc:   # noqa: BLE001
                        log.warning("recap_failed", error=str(exc))
                        text = f"RECAP failed: {exc}"
                    await self.say(text, "recap")
                self._spawn(_recap())
                reply = f"RECAP of the last {days} sessions started: the watchlist follows"
        elif action == "PLAN":
            if self.next_session_day(self.now()) is None:
                reply = "PLAN runs outside market hours; the 9:00/2:45 plans cover the session"
            else:
                async def _plan() -> None:
                    await self.job_next_plan(self.now())
                self._spawn(_plan())
                reply = "PLAN started: preview plans follow shortly"
        if cmd.source != "local" or action in ("NEED_CONFIRM", "STATUS", "PLAN"):
            await self.say(reply, "command_reply")
        return reply

    async def _owner_fill_command(self, action: str, arg: str) -> str:
        """FILLED NVDA 40 @ 132.55 / SOLD NVDA 40 @ 133.10 (co-pilot, another broker)."""
        import re

        m = re.match(r"^\s*([A-Z.]{1,6})\s+(\d+)\s*@\s*\$?(\d+(?:\.\d+)?)\s*$", arg)
        if not m:
            return f"usage: {action} NVDA 40 @ 132.55"
        t, q, px = m.group(1), int(m.group(2)), Decimal(m.group(3))
        if action == "FILLED":
            return await self.manual_fill(t, q, px, self.now(), "manual_ext")
        return await self.manual_exit(t, q, px, self.now())

    async def notifier_down(self, detail: str) -> None:
        """NF-1: approve and co-pilot entries stop (the risk manager blocks them while the
        notifier is red); tell the owner through the local fallback channel."""
        now = self.now()
        self.journal.incident(now, "NOTIFIER_DOWN", detail)
        if self.fallback is not None:
            with contextlib.suppress(Exception):
                await self.fallback.send(
                    "Trading notifier is down",
                    f"{to_et(now):%H:%M ET}: {detail}. Approve and co-pilot entries are paused "
                    "until the notifier is back (NF-1). Check `./run.sh status`.")

    def status_text(self) -> str:
        rs = self.risk.state
        pos = ", ".join(f"{p.ticker} {p.qty_open}sh stop {p.stop}" for p in
                        self.pm.open_positions()) or "none"
        return (f"Status {to_et(self.now()):%H:%M} · health {self.sup.overall()} · kill "
                f"{'ON' if rs.kill_switch else 'off'} · equity ${self.equity:,.2f} · day P&L "
                f"${rs.day_pnl:+.2f} · trades {rs.trades_total_today} · halted "
                f"{rs.halted or 'no'} · positions: {pos}")

    def status_dict(self) -> dict[str, Any]:
        """Also the console's cockpit (UI-1, UI-2) and its fallback via status_snapshots."""
        rs = self.risk.state
        now = self.now()

        def pos(p: ManagedPosition) -> dict[str, Any]:
            last = self.md.state[p.ticker].last_price if p.ticker in self.md.state else None
            entry = p.avg_entry
            r = entry - p.initial_stop
            unreal = (Decimal(str(last)) - entry) * p.qty_open if last is not None else None
            return {"position_id": p.position_id, "ticker": p.ticker, "qty": p.qty_open,
                    "style": p.style, "setup": p.setup, "route": p.route,
                    "entry": str(entry), "stop": str(p.stop), "t1": str(p.t1),
                    "last": last, "unrealized": str(unreal) if unreal is not None else None,
                    "r": float((Decimal(str(last)) - entry) / r) if last is not None and r > 0
                    else None, "opened_at": p.opened_at.isoformat()}
        return {"ts": now.isoformat(), "environment": self.cfg.mode.environment,
                "health": {k: v.level for k, v in self.sup.health.items()},
                "health_detail": {k: v.detail for k, v in self.sup.health.items()},
                "kill_switch": rs.kill_switch, "equity": str(self.equity),
                "day_start_equity": str(rs.day_start_equity),
                "day_pnl": str(rs.day_pnl), "trades_today": rs.trades_total_today,
                "max_trades": self.cfg.risk.max_trades_per_day,
                "loss_limit_pct": self.cfg.risk.daily_loss_limit_pct,
                "halted": rs.halted, "paused": sorted(rs.paused_styles),
                "regime": self.regime, "window": self.windows.window_at(now),
                "positions": [pos(p) for p in self.pm.open_positions()],
                "locks": [{"ticker": x["ticker"], "type": x["type"]}
                          for x in self.compliance.locked_list(now)],
                "options_open": len(self.options.book.open) if self.options is not None else 0}

    # ===================================================================================
    # reconcile and helpers
    # ===================================================================================
    async def reconcile(self) -> list[str]:
        expected: dict[str, int] = {}
        for p in self.pm.open_positions():
            if p.route in ("broker", "manual"):   # CP-2: the owner's Alpaca buys are expected
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


def idea_views(plans: dict[str, GamePlan], screens: dict[str, dict[str, Any]],
               compact: dict[str, dict[str, Any]], off: dict[str, str]) -> list[Any]:
    """Options views for the 4:15 PM ideas: each long pick (swing structure) and up to three
    bearish candidates (BR-3: close near the low of the range, not in an uptrend)."""
    from trader.options.strategist import View

    views: list[Any] = []
    seen: set[str] = set()
    for plan in plans.values():
        for item in plan.watch:
            scr = screens.get(item.ticker)
            if scr is None or item.ticker in seen or item.ticker in off:
                continue
            seen.add(item.ticker)
            views.append(View(item.ticker, "bull", float(scr["close"]), float(item.stop),
                              float(item.target_1), None, "swing", 0, "idea", item.setup,
                              False, True, window="next"))
    bears = sorted((t for t, scr in screens.items()
                    if t not in off and scr.get("range_pos") is not None
                    and scr["range_pos"] <= 0.10 and not scr.get("trend_up")),
                   key=lambda t: screens[t]["range_pos"])[:3]
    for t in bears:
        atr = (compact.get(t, {}).get("levels") or {}).get("atr14d")
        close = float(screens[t]["close"])
        if not atr:
            continue
        views.append(View(t, "bear", close, close + float(atr), close - 1.5 * float(atr),
                          close - 3 * float(atr), "swing", 0, "idea", "CLOSE_NEAR_LOW", False,
                          True, window="next"))
    return views
