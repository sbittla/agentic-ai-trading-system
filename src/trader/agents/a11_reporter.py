"""A11 Reporter (A11-1..A11-3, SEC-15, WS-6, REPORT_TEMPLATES.md).

Every figure comes from a journal query (A11-3). Qwen writes only the three narrative fields;
a narrative containing any number that is not in the computed metrics is rejected and omitted.
Grades A/B/C come from code.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import and_, select

from trader.agents.a12_compliance import ComplianceGuard
from trader.config import Config
from trader.interfaces import LLMClient
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.base import load_prompt
from trader.metrics import (
    benchmark,
    by,
    daily_returns,
    max_drawdown_pct,
    risk_adjusted,
    standards,
    statistical_gate,
    summarize,
)
from trader.models.llm_schemas import ReportNarrativeOut
from trader.timeutil import ET, et_datetime

log = structlog.get_logger(__name__)
NUM_RE = re.compile(r"[-+]?\$?\d[\d,]*\.?\d*%?")


def _norm_num(tok: str) -> str:
    t = tok.replace("$", "").replace(",", "").replace("%", "").lstrip("+")
    try:
        v = float(t)
    except ValueError:
        return tok
    return f"{v:.4f}".rstrip("0").rstrip(".")


def numbers_ok(narrative: str, allowed: set[str]) -> bool:
    """A11-3: every number in the narrative must appear in the metrics."""
    for tok in NUM_RE.findall(narrative):
        if _norm_num(tok) not in allowed:
            return False
    return True


def allowed_numbers(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= allowed_numbers(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            out |= allowed_numbers(v)
    elif isinstance(obj, int | float) and not isinstance(obj, bool):
        out.add(_norm_num(str(obj)))
    elif isinstance(obj, str):
        for tok in NUM_RE.findall(obj):
            out.add(_norm_num(tok))
    return out


class Reporter:
    def __init__(self, cfg: Config, journal: SqlJournal, llm: LLMClient | None,
                 compliance: ComplianceGuard, out_dir: str | Path,
                 now: Callable[[], datetime]) -> None:
        self.cfg = cfg
        self.journal = journal
        self.llm = llm
        self.compliance = compliance
        self.out_dir = Path(out_dir)
        self.now = now

    def tag(self) -> str:
        return "[LIVE]" if self.cfg.mode.environment == "live" else "[PAPER]"

    # -- queries ----------------------------------------------------------------------------
    def _day_bounds(self, d: date, until: time | None = None) -> tuple[datetime, datetime]:
        start = et_datetime(d, time(0, 0))
        end = et_datetime(d, until) if until else start + timedelta(days=1)
        return start, end

    def trades(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        return self.journal.closed_positions(since, until)

    def grade(self, t: dict[str, Any]) -> str:
        """REPORT_TEMPLATES §5: A = clean, B = near a limit (chase > 0.15 ATR5), C = unplanned
        or a rule exception (override)."""
        if not t.get("planned") or t.get("override"):
            return "C"
        st = t.get("state_json") or {}
        atr5 = float(st.get("atr5") or 0)
        trig = float(st.get("trigger") or 0)
        if atr5 > 0 and float(t.get("avg_entry") or 0) - trig > 0.15 * atr5:
            return "B"
        return "A"

    def no_trade_log(self, since: datetime, until: datetime) -> dict[str, int]:
        rows = self.journal.decisions_between(since, until)
        return dict(Counter(r["reason_code"] for r in rows if r["result"] == "REJECT"))

    def commands(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        return self.journal.query(select(s.commands).where(and_(s.commands.c.ts >= since,
                                                                s.commands.c.ts < until)))

    def breaches(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        return self.journal.query(select(s.incidents).where(and_(
            s.incidents.c.ts >= since, s.incidents.c.ts < until,
            s.incidents.c.type == "RULE_BREACH")))

    def equity_curve(self, since: datetime, until: datetime) -> list[float]:
        rows = self.journal.query(select(s.equity).where(and_(s.equity.c.ts >= since,
                                                              s.equity.c.ts < until))
                                  .order_by(s.equity.c.id))
        return [float(r["equity"]) for r in rows]

    def daily_equity(self, first: date, last: date) -> list[float]:
        """End-of-day equity per session from ``daily_metrics`` (PA-1)."""
        rows = self.journal.query(select(s.daily_metrics.c.equity).where(and_(
            s.daily_metrics.c.date >= first, s.daily_metrics.c.date <= last))
            .order_by(s.daily_metrics.c.date))
        return [float(r["equity"]) for r in rows if r["equity"] is not None]

    def spy_closes(self, first: date, last: date) -> tuple[float | None, float | None]:
        """PA-2: SPY close before ``first`` and the latest close up to ``last``, from the
        level map (``prior_close`` of each session)."""
        rows = self.journal.query(select(s.levels.c.date, s.levels.c.prior_close).where(and_(
            s.levels.c.ticker == "SPY", s.levels.c.date >= first,
            s.levels.c.date <= last + timedelta(days=4))).order_by(s.levels.c.date))
        closes = [float(r["prior_close"]) for r in rows if r["prior_close"]]
        return (closes[0], closes[-1]) if len(closes) >= 2 else (None, None)

    def analytics(self, first: date, last: date, trades: list[dict[str, Any]],
                  history: list[dict[str, Any]]) -> dict[str, Any]:
        """PA-1..PA-3 and SV-1..SV-3 for a report period."""
        eq = self.daily_equity(first, last)
        rets = daily_returns(eq)
        ret_pct = (eq[-1] / eq[0] - 1) * 100 if len(eq) >= 2 and eq[0] > 0 else 0.0
        rs = [float(t.get("r_multiple") or 0) for t in trades]
        all_rs = [float(t.get("r_multiple") or 0) for t in history]
        r = self.cfg.risk
        return {"risk_adjusted": risk_adjusted(rets, rs),
                "benchmark": benchmark(*self.spy_closes(first, last), ret_pct),
                "statistical": statistical_gate(all_rs, r.risk_per_trade_pct,
                                                r.monthly_drawdown_limit_pct)}

    # -- reports ----------------------------------------------------------------------------
    async def daily(self, d: date, kind: str = "daily") -> dict[str, Any]:
        """10:30 morning summary (kind='morning') or 4:30 PM daily review (A11-1)."""
        since, until = self._day_bounds(d, time(10, 31) if kind == "morning" else None)
        trades = self.trades(since, until)
        m = summarize(trades)
        curve = self.equity_curve(since, until)
        m["max_intraday_dd_pct"] = max_drawdown_pct(curve)
        rows = [{"ticker": t["ticker"], "setup": t["setup"], "window": t["window"],
                 "r": round(float(t["r_multiple"] or 0), 2), "grade": self.grade(t),
                 "exit": t["exit_reason"], "pnl": round(float(t["pnl"] or 0), 2),
                 "route": t["route"]} for t in trades]
        nt = self.no_trade_log(since, until)
        cmds = self.commands(since, until)
        rejected_senders = sorted({c["sender"] for c in cmds if not c["accepted"]})
        breaches = self.breaches(since, until)
        locks = self.compliance.locked_list(self.now())
        data = {"date": d.isoformat(), "kind": kind, "metrics": m, "trades": rows,
                "no_trade_log": nt, "rule_breaches": len(breaches),
                "commands": [f"{c['text']} ({'ok' if c['accepted'] else 'rejected'})"
                             for c in cmds],
                "rejected_senders": rejected_senders,
                "locks": [{"ticker": x["ticker"], "type": x["type"],
                           "until": x["until"].astimezone(ET).date().isoformat()}
                          for x in locks],
                "ira_warning": self.compliance.ira_warning(self.now())}
        data["narrative"] = await self._narrative(data) if kind == "daily" else None
        if kind == "daily":
            self.journal.upsert(s.daily_metrics, {"date": d}, {
                "trades": m["trades"], "wins": m["wins"], "losses": m["losses"],
                "pnl": str(m["pnl"]), "peak_pnl": None, "max_dd": None,
                "expectancy_r": m["expectancy_r"], "rule_breaches": len(breaches),
                "equity": str(curve[-1]) if curve else None})
        data["text"] = self.render_daily(data)
        self._write(f"{d.isoformat()}_{kind}", data)
        return data

    def render_daily(self, data: dict[str, Any]) -> str:
        m = data["metrics"]
        title = "Morning summary" if data["kind"] == "morning" else "Daily review"
        lines = [f"{self.tag()} {title} {data['date']}",
                 f"P&L {_money(m['pnl'])} · {m['trades']} trades ({m['wins']}W {m['losses']}L) "
                 f"· {m['total_r']:+.2f}R total · max intraday DD "
                 f"-{abs(m['max_intraday_dd_pct']):.2f}%"]
        if data["trades"]:
            lines.append("Trades:")
            for t in data["trades"]:
                lines.append(f" {t['ticker']:<5} {t['setup']:<20} {t['window']:<9} "
                             f"{t['r']:+.2f}R grade {t['grade']} exit {t['exit']} "
                             f"{_money(t['pnl'])}" + (" [paper]" if t["route"] == "paper" else ""))
        else:
            lines.append("No trades (no qualifying setup is a normal outcome, NT-2).")
        nt = data["no_trade_log"]
        if nt:
            lines.append(f"No-trade log: {sum(nt.values())} rejected — " + ", ".join(
                f"{k} x{v}" for k, v in sorted(nt.items(), key=lambda x: -x[1])))
        lines.append(f"Rule breaches: {data['rule_breaches'] or 'none'} · Commands: "
                     f"{', '.join(data['commands']) or 'none'} · Rejected senders: "
                     f"{', '.join(data['rejected_senders']) or 'none'}")
        if data["locks"]:
            lines.append("Locks: " + ", ".join(f"{x['ticker']} {x['type']} until {x['until']}"
                                               for x in data["locks"]))
        if data.get("ira_warning"):
            lines.append(data["ira_warning"])
        nar = data.get("narrative")
        if nar:
            lines += [f"What went well: {nar['what_went_well']}",
                      f"What to fix: {nar['what_to_fix']}",
                      f"Tomorrow focus: {nar['tomorrow_focus']}"]
        return "\n".join(lines)

    async def weekly(self, d: date) -> dict[str, Any]:
        """Friday review with the performance standards (A11-2, §6.4)."""
        monday = d - timedelta(days=d.weekday())
        since, until = et_datetime(monday, time(0, 0)), et_datetime(d, time(0, 0)) + timedelta(
            days=1)
        trades = self.trades(since, until)
        all_trades = self.journal.closed_positions()
        curve = self.equity_curve(et_datetime(d.replace(day=1), time(0, 0)), until)
        breaches = len(self.breaches(since, until))
        data = {"week_ending": d.isoformat(), "results": summarize(trades),
                "by_setup": by(trades, "setup"), "by_window": by(trades, "window"),
                "by_ticker": by(trades, "ticker"),
                "setup_last20": {k: v for k, v in by(all_trades[-200:], "setup").items()},
                "standards": standards(all_trades, breaches, max_drawdown_pct(curve),
                                       self.cfg.risk.monthly_drawdown_limit_pct),
                "pipeline": self.no_trade_log(since, until),
                **self.analytics(d.replace(day=1), d, trades, all_trades),
                "overrides": sum(1 for t in trades if t.get("override")),
                "locks": [x["ticker"] for x in self.compliance.locked_list(self.now())]}
        data["text"] = (f"{self.tag()} Weekly review week ending {d}\n"
                        + json.dumps({k: data[k] for k in ("results", "standards",
                                                           "risk_adjusted", "benchmark",
                                                           "statistical")}, default=str,
                                     indent=1))
        self._write(f"{d.isoformat()}_weekly", data)
        return data

    async def monthly(self, year: int, month: int) -> dict[str, Any]:
        first = date(year, month, 1)
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        since, until = et_datetime(first, time(0, 0)), et_datetime(nxt, time(0, 0))
        trades = self.trades(since, until)
        curve = self.equity_curve(since, until)
        data = {"month": f"{year}-{month:02d}", "results": summarize(trades),
                "by_setup": by(trades, "setup"), "max_dd_pct": max_drawdown_pct(curve),
                "monthly_limit_pct": self.cfg.risk.monthly_drawdown_limit_pct,
                "wash_sale_report": self.compliance.monthly_report(year, month),
                **self.analytics(first, nxt - timedelta(days=1), trades,
                                 self.journal.closed_positions()),
                "gates": sorted(self.journal.gates_passed())}
        data["text"] = f"{self.tag()} Monthly review {data['month']}\n" + json.dumps(
            {k: data[k] for k in ("results", "risk_adjusted", "benchmark", "statistical")},
            default=str)
        self._write(f"{year}-{month:02d}_monthly", data)
        exp = self.out_dir / f"{year}-{month:02d}_wash_sales.json"
        exp.write_text(json.dumps(data["wash_sale_report"], indent=1, default=str),
                       encoding="utf-8")
        return data

    # -- narrative ---------------------------------------------------------------------------
    async def _narrative(self, data: dict[str, Any]) -> dict[str, str] | None:
        if self.llm is None:
            return None
        version = self.cfg.llm.prompt_versions.get("a11", "v1")
        payload = {k: v for k, v in data.items() if k not in ("text", "narrative")}
        res = await self.llm.structured(agent="a11", prompt_version=version,
                                        system=load_prompt("a11", version), data=payload,
                                        schema=ReportNarrativeOut,
                                        timeout_s=self.cfg.llm.timeout_sec)
        if not res.ok or not isinstance(res.value, ReportNarrativeOut):
            return None
        nar = res.value.model_dump()
        allowed = allowed_numbers(payload)
        if not all(numbers_ok(v, allowed) for v in nar.values()):
            log.warning("narrative_rejected_numbers")   # A11-3
            return None
        return nar

    def _write(self, name: str, data: dict[str, Any]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / f"{name}.json").write_text(json.dumps(data, indent=1, default=str),
                                                   encoding="utf-8")
        body = html.escape(str(data.get("text", "")))
        (self.out_dir / f"{name}.html").write_text(
            "<!doctype html><meta charset='utf-8'><title>Trading report</title>"
            f"<pre style='font:14px ui-monospace,monospace'>{body}</pre>", encoding="utf-8")
        self.journal.insert(s.events, {"ts": self.now(), "type": "ReportPublished",
                                       "payload": {"kind": name, "path": str(self.out_dir)}})


def _money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"
