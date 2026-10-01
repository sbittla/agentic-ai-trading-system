"""Replay and backtest engine (BACKTEST_ENGINE.md §1-§6).

Runs the same agent code as live by swapping the interfaces: ``ReplayClock``,
``InMemoryHistory`` / Parquet, ``SimBroker``, ``CachedLLM`` or ``RulesLLM`` and a scripted
approval notifier. Event loop per BACKTEST_ENGINE §2; no agent sees a bar before its end time
and no news before its time (BT-2). Each run writes a self-contained folder.
"""

from __future__ import annotations

import asyncio
import csv
import json
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml

from trader.brokers.sim import FILL_MODEL_VERSION, SimBroker
from trader.calendar import MacroCalendar, MarketCalendar
from trader.config import Config
from trader.engine import TradingEngine
from trader.interfaces import LLMClient
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.metrics import by, max_drawdown_pct, summarize
from trader.models.messages import ApprovalResponse, Bar, EventFlags
from trader.timeutil import et_date, et_datetime, to_et

log = structlog.get_logger(__name__)


class ReplayClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        if t > self._now:
            self._now = t

    def set(self, t: datetime) -> None:
        self._now = t

    async def sleep_until(self, t: datetime) -> None:
        self.advance_to(t)


class InMemoryHistory:
    """HistoricalData fake backed by lists of bars (no look-ahead: callers pass ``end``)."""

    def __init__(self, daily: dict[str, list[Bar]], minutes: Iterable[Bar] = ()) -> None:
        self.daily = daily
        self.minutes: dict[str, list[Bar]] = defaultdict(list)
        for b in minutes:
            self.minutes[b.symbol].append(b)

    async def daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        return [b for b in self.daily.get(symbol, []) if start <= et_date(b.ts) < end]

    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        return [b for b in self.minutes.get(symbol, []) if start <= b.ts < end]


class ScriptedApprovals:
    """Notifier for replays: approvals answered by policy; messages collected."""

    def __init__(self, policy: Literal["yes", "no", "timeout"] = "yes") -> None:
        self.policy = policy
        self.sent: list[tuple[str, str]] = []
        self.requests: list[str] = []

    async def send(self, text: str, *, kind: str) -> None:
        self.sent.append((kind, text))

    async def request_approval(self, proposal_id: str, text: str, code: str,
                               timeout_s: int) -> ApprovalResponse:
        self.requests.append(proposal_id)
        self.sent.append(("approval", text))
        return ApprovalResponse(proposal_id=proposal_id, answer=self.policy)


@dataclass
class RunResult:
    out_dir: Path
    metrics: dict[str, Any]
    trades: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    messages: list[str] = field(default_factory=list)
    engine: TradingEngine | None = None


async def run_replay(cfg: Config, *, minutes: dict[date, list[Bar]],
                     daily: dict[str, list[Bar]], llm: LLMClient, out_dir: str | Path,
                     news: dict[date, list[EventFlags]] | None = None,
                     approvals: Literal["yes", "no", "timeout"] = "yes",
                     equity: Decimal = Decimal("100000"), macro: MacroCalendar | None = None,
                     calendar: MarketCalendar | None = None,
                     seed_minutes: Iterable[Bar] = (), name: str = "replay",
                     keep_engine: bool = False) -> RunResult:
    """Replay recorded (or synthetic) days through the full system."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "journal.db"
    if db.exists():
        db.unlink()
    days = sorted(minutes)
    cal = calendar or MarketCalendar()
    mac = macro or _default_macro(cfg)
    clock = ReplayClock(et_datetime(days[0], time(4, 0)).astimezone(UTC))
    journal = SqlJournal(f"sqlite:///{db.as_posix()}")
    broker = SimBroker(clock.now, equity=equity)
    notifier = ScriptedApprovals(approvals)
    history = InMemoryHistory(daily, seed_minutes)
    eng = TradingEngine(cfg, now=clock.now, calendar=cal, macro=mac, broker=broker,
                        journal=journal, llm=llm, notifier=notifier, history=history,
                        require_quotes=False, reports_dir=out / "reports", sim_mode=True)
    for comp in ("alpaca", "data", "ollama", "postgres", "clock", "reconcile"):
        eng.sup.set_health(comp, "green", "replay", clock.now())
    await eng.start()
    _warm(eng, seed_minutes)
    news = news or {}

    async def step(t: datetime) -> None:
        clock.advance_to(t)
        await eng.tick(clock.now())
        await eng.settle()

    for d in days:
        for hh, mm in ((7, 0), (7, 15), (8, 0), (9, 0)):
            await step(et_datetime(d, time(hh, mm)).astimezone(UTC))
            if (hh, mm) == (7, 15):
                for nf in news.get(d, []):
                    eng.on_news(nf)
        bars = minutes[d]
        for ts, group in groupby(bars, key=lambda b: b.ts):
            batch = list(group)
            clock.advance_to(ts + timedelta(minutes=1))
            for b in batch:
                await eng.on_bar(b)
            for b in batch:
                broker.match(b)
            for u in broker.drain_updates():
                await eng.on_order_update(u)
            await eng.tick(clock.now())
            await eng.settle()
            for u in broker.drain_updates():
                await eng.on_order_update(u)
        close = cal.session(d).close
        t = clock.now()
        while t < close:
            t = t + timedelta(minutes=1)
            await step(t)
            for u in broker.drain_updates():
                await eng.on_order_update(u)
        for hh, mm in ((16, 0), (16, 30), (17, 0), (17, 30)):
            await step(et_datetime(d, time(hh, mm)).astimezone(UTC))
            for u in broker.drain_updates():
                await eng.on_order_update(u)
    res = _outputs(cfg, eng, journal, out, name, days, broker)
    res.messages = [t for _, t in notifier.sent]
    if keep_engine:
        res.engine = eng
    else:
        journal.dispose()
    return res


def _default_macro(cfg: Config) -> MacroCalendar:
    path = Path(cfg.windows.macro_calendar_file)
    return MacroCalendar.load(path) if path.exists() else MacroCalendar.empty()


def _warm(eng: TradingEngine, seed_minutes: Iterable[Bar]) -> None:
    by_sym: dict[str, dict[date, list[Bar]]] = defaultdict(lambda: defaultdict(list))
    for b in seed_minutes:
        by_sym[b.symbol][et_date(b.ts)].append(b)
    for sym, days in by_sym.items():
        if sym in eng.md.state:
            eng.md.seed_history(sym, [days[d] for d in sorted(days)])


def _outputs(cfg: Config, eng: TradingEngine, journal: SqlJournal, out: Path, name: str,
             days: list[date], broker: SimBroker) -> RunResult:
    trades = journal.closed_positions()
    decisions = journal.rows(s.decisions, order_by="id")
    curve = [float(r["equity"]) for r in journal.rows(s.equity, order_by="id")]
    m = summarize(trades)
    m.update({"max_drawdown_pct": max_drawdown_pct(curve),
              "final_equity": str(broker.equity()), "fees": str(broker.fees),
              "by_setup": by(trades, "setup"), "by_ticker": by(trades, "ticker"),
              "by_window": by(trades, "window"),
              "pipeline": {"setups": eng.stats.setups, "proposals": eng.stats.proposals,
                           "reviews": eng.stats.reviews, "submitted": eng.stats.submitted,
                           "rejected": eng.stats.rejected},
              "rejections": dict(sorted(_count(r["reason_code"] for r in decisions
                                               if r["result"] in ("REJECT", "NO_TRADE"))
                                        .items(), key=lambda x: -x[1]))})
    (out / "metrics.json").write_text(json.dumps(m, indent=1, default=str), encoding="utf-8")
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        cols = ["position_id", "ticker", "setup", "style", "window", "opened_at", "closed_at",
                "qty", "avg_entry", "avg_exit", "pnl", "r_multiple", "exit_reason", "route"]
        w.writerow(cols)
        for t in trades:
            w.writerow([t.get(c) for c in cols])
    run = {"name": name, "code_commit": _git_commit(), "config_sha256": _cfg_hash(cfg),
           "fill_model_version": FILL_MODEL_VERSION,
           "prompt_versions": dict(cfg.llm.prompt_versions), "model": eng.llm.model,
           "data_range": [days[0].isoformat(), days[-1].isoformat()], "days": len(days)}
    (out / "run.yaml").write_text(yaml.safe_dump(run, sort_keys=False), encoding="utf-8")
    log_lines = [_decision_line(r) for r in decisions]
    (out / "decisions.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    _report_html(out, m, trades)
    return RunResult(out_dir=out, metrics=m, trades=trades, decisions=decisions)


def _decision_line(r: dict[str, Any]) -> str:
    ts = r["ts"].astimezone(UTC).isoformat() if r["ts"] else "-"
    return (f"{ts} {r['proposal_id']} {r['ticker']} {r['setup']} {r['window']} {r['result']} "
            f"{r['reason_code']} qty={r['qty']}")


def diff_runs(prev: Path, cur: Path) -> str:
    """RP-3: decisions that changed since the previous run of the same days."""
    a = (prev / "decisions.log").read_text(encoding="utf-8").splitlines() if (
        prev / "decisions.log").exists() else []
    b = (cur / "decisions.log").read_text(encoding="utf-8").splitlines()
    removed = [x for x in a if x not in set(b)]
    added = [x for x in b if x not in set(a)]
    text = (f"# Decision diff\n\nprevious: {prev}\ncurrent: {cur}\n\n"
            f"- unchanged: {len(set(a) & set(b))}\n- removed: {len(removed)}\n"
            f"- added: {len(added)}\n\n## Removed\n" + "\n".join(f"- {x}" for x in removed)
            + "\n\n## Added\n" + "\n".join(f"- {x}" for x in added) + "\n")
    (cur / "diff_vs_previous.md").write_text(text, encoding="utf-8")
    return text


def _report_html(out: Path, m: dict[str, Any], trades: list[dict[str, Any]]) -> None:
    rows = "".join(
        f"<tr><td>{t['ticker']}</td><td>{t['setup']}</td><td>{t['window']}</td>"
        f"<td>{to_et(t['opened_at']):%Y-%m-%d %H:%M}</td><td>{float(t['r_multiple'] or 0):+.2f}"
        f"</td><td>{float(t['pnl'] or 0):+.2f}</td><td>{t['exit_reason']}</td></tr>"
        for t in trades)
    head = {k: m[k] for k in ("trades", "win_rate", "expectancy_r", "profit_factor", "pnl",
                              "max_drawdown_pct", "final_equity")}
    html = ("<!doctype html><meta charset='utf-8'><title>Replay report</title>"
            "<style>body{font:14px system-ui;margin:16px}td,th{padding:2px 8px}"
            "table{border-collapse:collapse}tr:nth-child(even){background:#f4f4f4}</style>"
            f"<h1>Replay report</h1><pre>{json.dumps(head, indent=1, default=str)}</pre>"
            "<table><tr><th>Ticker</th><th>Setup</th><th>Window</th><th>Opened</th><th>R</th>"
            f"<th>P&amp;L</th><th>Exit</th></tr>{rows}</table>")
    (out / "report.html").write_text(html, encoding="utf-8")


def _count(xs: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for x in xs:
        out[x] += 1
    return dict(out)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,  # noqa: S607
                              text=True, timeout=5, check=False).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _cfg_hash(cfg: Config) -> str:
    import hashlib

    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()


def run(coro: Any) -> Any:
    return asyncio.run(coro)
