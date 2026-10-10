"""Console data sources. Every source fails soft: a page shows what is missing instead of
failing (UI-27). Reads: the journal (``console_rw``: SELECT on every table), trader-core's
internal API, Ollama, and the read-only ``reports/``, ``runs/`` and ``config/`` mounts.
Writes: only owner commands to the outbox, notes and chat history (UI-C2)."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import and_, delete, desc, select

from trader.bus import SqlOutbox
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.timeutil import ET, et_date, to_et

log = structlog.get_logger(__name__)

# Owner commands the console may send (SEC-1 allow-list; trader-core decides again).
ALLOWED = {
    "STATUS",
    "PLAN",
    "KILL",
    "PAUSE",
    "UNPAUSE",
    "ACK",
    "FLATTEN",
    "RESUME",
    "CONFIRM",
    "YES",
    "NO",
    "ALERTS",
    "FILLED",
    "SOLD",
    "OPT",
    "OPTIONS",
}
DESTRUCTIVE = {"FLATTEN", "RESUME", "UNPAUSE"}
ARG_RE = re.compile(r"^[A-Za-z0-9 .@:_-]{0,40}$")


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ET).astimezone(UTC)


@dataclass(frozen=True)
class Paths:
    reports: Path
    runs: Path
    config: Path
    alerts_db: Path | None = None


class CoreClient:
    """trader-core's internal read-only API (``CORE_API_URL``), 3 s timeout."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=3, transport=transport)
        self.last_error = ""

    async def get(self, path: str, **params: Any) -> Any:
        try:
            r = await self._client.get(
                self.base + path, params={k: v for k, v in params.items() if v}
            )
            if r.status_code != 200:
                self.last_error = f"{path}: {r.json().get('error', r.status_code)}"
                return None
            return r.json()
        except Exception as exc:  # noqa: BLE001 - shown on the page (UI-27)
            self.last_error = f"trader-core API unreachable ({type(exc).__name__})"
            return None


class ConsoleData:
    def __init__(
        self,
        journal: SqlJournal,
        core: CoreClient,
        cfg: Any,
        paths: Paths,
        *,
        ollama_url: str = "",
        now: Any = None,
        ollama_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.j = journal
        self.core = core
        self.cfg = cfg
        self.paths = paths
        self.ollama_url = ollama_url.rstrip("/")
        self.now = now or (lambda: datetime.now(UTC))
        self.outbox = SqlOutbox(journal, "console")
        self._ollama = httpx.AsyncClient(timeout=3, transport=ollama_transport)

    # -- helpers ---------------------------------------------------------------------------------
    def today(self) -> date:
        return et_date(self.now())

    def _since(
        self,
        table: Any,
        col: str,
        since: datetime,
        limit: int = 500,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            q = select(table).where(table.c[col] >= since)
            for k, v in (where or {}).items():
                q = q.where(table.c[k] == v)
            return self.j.query(q.order_by(desc(table.c[col])).limit(limit))
        except Exception as exc:  # noqa: BLE001
            log.warning("console_query_failed", table=table.name, error=str(exc))
            return []

    def rows(
        self, table: Any, where: dict[str, Any] | None = None, **kw: Any
    ) -> list[dict[str, Any]]:
        try:
            return self.j.rows(table, where, **kw)
        except Exception as exc:  # noqa: BLE001
            log.warning("console_query_failed", table=table.name, error=str(exc))
            return []

    # -- UI-1..UI-4b cockpit -------------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        st = await self.core.get("/status")
        if st is not None:
            return {**st, "source": "live"}
        snap = self.rows(s.status_snapshots, order_by="ts", descending=True, limit=1)
        if snap:
            return {
                **(snap[0]["json"] or {}),
                "source": "snapshot",
                "snapshot_ts": snap[0]["ts"].isoformat(),
            }
        return {"source": "none", "error": self.core.last_error}

    def plans(self, d: date) -> list[dict[str, Any]]:
        return self.rows(s.game_plans, {"date": d}, order_by="id")

    def ideas(self, plan_for: date | None = None) -> list[dict[str, Any]]:
        if plan_for is None:
            latest = self.rows(s.next_day_ideas, order_by="plan_for", descending=True, limit=1)
            if not latest:
                return []
            plan_for = latest[0]["plan_for"]
        rows = self.rows(s.next_day_ideas, {"plan_for": plan_for}, order_by="id")
        if not rows:
            return []
        last_run = max(r["run_at"] for r in rows)
        return [r for r in rows if r["run_at"] == last_run]

    def incidents_open(self, days: int = 3) -> list[dict[str, Any]]:
        since = self.now() - timedelta(days=days)
        return [r for r in self._since(s.incidents, "ts", since, 200) if r["resolved_at"] is None]

    def heartbeats(self) -> dict[str, datetime]:
        rows = self._since(
            s.outbox, "created_at", self.now() - timedelta(hours=1), 400, {"channel": "heartbeat"}
        )
        out: dict[str, datetime] = {}
        for r in rows:
            svc = (r.get("payload") or {}).get("service") or r["source"]
            out.setdefault(svc, r["created_at"])
        return out

    async def ollama(self) -> dict[str, Any]:
        info: dict[str, Any] = {"model": getattr(self.cfg.llm, "model", "?")}
        since = day_start(self.today())
        lat = sorted(
            r["latency_ms"]
            for r in self._since(s.llm_calls, "ts", since, 2000)
            if r.get("latency_ms")
        )
        info["calls_today"] = len(lat)
        info["p95_ms"] = lat[int(len(lat) * 0.95) - 1] if lat else None
        if not self.ollama_url:
            info["error"] = "OLLAMA_URL not set"
            return info
        try:
            r = await self._ollama.get(self.ollama_url + "/api/ps")
            models = r.json().get("models") or []
            info["loaded"] = [m.get("name") for m in models]
            info["vram_gb"] = sum(m.get("size_vram") or 0 for m in models) / 1e9
            info["size_gb"] = sum(m.get("size") or 0 for m in models) / 1e9
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"Ollama unreachable ({type(exc).__name__})"
        return info

    # -- UI-9..UI-11 trade story and approvals ----------------------------------------------------
    def pending_approvals(self) -> list[dict[str, Any]]:
        now = self.now()
        ttl = timedelta(seconds=self.cfg.notifications.approval_timeout_sec)
        out = []
        for a in self._since(s.approvals, "sent_at", now - ttl, 50):
            if a["answered_at"] is not None or a["sent_at"] + ttl < now:
                continue
            out.append(
                {
                    "proposal_id": a["proposal_id"],
                    "sent_at": a["sent_at"],
                    "expires": a["sent_at"] + ttl,
                    **self._proposal_brief(a["proposal_id"]),
                }
            )
        return out  # the one-time code is never rendered (UI-C3)

    def approvals_today(self) -> list[dict[str, Any]]:
        rows = self._since(s.approvals, "sent_at", day_start(self.today()), 100)
        return [
            {
                "proposal_id": r["proposal_id"],
                "sent_at": r["sent_at"],
                "answered_at": r["answered_at"],
                "answer": r["answer"] or "expired"
                if r["answered_at"] is None
                and r["sent_at"] + timedelta(seconds=self.cfg.notifications.approval_timeout_sec)
                < self.now()
                else r["answer"] or "pending",
                **self._proposal_brief(r["proposal_id"]),
            }
            for r in rows
        ]

    def _proposal_brief(self, pid: str) -> dict[str, Any]:
        p = self.rows(s.proposals, {"proposal_id": pid}, limit=1)
        d = self.rows(s.decisions, {"proposal_id": pid}, order_by="id", descending=True, limit=1)
        js = (p[0]["json"] or {}) if p else {}
        return {
            "ticker": p[0]["ticker"] if p else (d[0]["ticker"] if d else "?"),
            "setup": js.get("setup") or (d[0]["setup"] if d else ""),
            "entry": js.get("entry"),
            "stop": js.get("stop"),
            "t1": js.get("target_1"),
            "qty": d[0]["qty"] if d else None,
            "risk_usd": d[0]["risk_usd"] if d else None,
            "reasoning": js.get("reasoning") or js.get("thesis") or "",
        }

    def story(self, pid: str) -> dict[str, Any]:
        prop = self.rows(s.proposals, {"proposal_id": pid}, limit=1)
        setup_id = prop[0]["setup_id"] if prop else None
        if setup_id is None:
            lat = self.rows(s.latency, {"proposal_id": pid}, limit=1)
            setup_id = lat[0]["setup_id"] if lat else None
        orders = self.rows(s.orders, {"proposal_id": pid}, order_by="ts")
        oids = {o["client_order_id"] for o in orders}
        fills = (
            [f for f in self.rows(s.fills, order_by="ts") if f["client_order_id"] in oids]
            if oids
            else []
        )
        positions = [
            p
            for p in self.rows(s.positions)
            if (p.get("state_json") or {}).get("proposal_id") == pid or p["position_id"] == pid
        ]
        return {
            "proposal_id": pid,
            "setup": self.rows(s.setups, {"id": setup_id}, limit=1) if setup_id else [],
            "proposal": prop,
            "review": self.rows(s.reviews, {"proposal_id": pid}),
            "compliance": self.rows(s.compliance_checks, {"proposal_id": pid}),
            "decisions": self.rows(s.decisions, {"proposal_id": pid}, order_by="id"),
            "approvals": [
                {k: v for k, v in a.items() if k != "code"}
                for a in self.rows(s.approvals, {"proposal_id": pid})
            ],
            "orders": orders,
            "fills": fills,
            "positions": positions,
            "latency": self.rows(s.latency, {"proposal_id": pid}),
            "notes": self.rows(s.owner_notes, {"ref_id": pid}, order_by="ts"),
        }

    def recent_proposals(self, days: int = 7) -> list[dict[str, Any]]:
        return self._since(s.decisions, "ts", self.now() - timedelta(days=days), 200)

    # -- UI-12, UI-13 commands ------------------------------------------------------------------
    async def command(self, name: str, arg: str = "", wait_s: float = 4.0) -> dict[str, Any]:
        """Publish an owner command (outbox ``inbound_cmd``, source console) and wait briefly
        for trader-core's CommandAuthority to journal its verdict."""
        name = name.upper().strip()
        arg = arg.strip()
        if name not in ALLOWED or not ARG_RE.match(arg):
            return {"ok": False, "reason": "not an allowed console command (SEC-1)"}
        sent = self.now()
        await self.outbox.publish(
            "inbound_cmd",
            {
                "name": name,
                "arg": arg,
                "sender": "console",
                "received_at": sent.isoformat(),
                "source": "console",
            },
        )
        text = f"{name} {arg}".strip()
        loop = asyncio.get_running_loop()
        end = loop.time() + wait_s
        while loop.time() < end:
            for r in self._since(
                s.commands, "ts", sent - timedelta(seconds=1), 20, {"sender": "console"}
            ):
                if (r["text"] or "").strip() == text:
                    reason = r["reason"] or ""
                    if "confirmation code issued" in reason:  # the code goes to the phone
                        reason = (
                            "confirmation code sent to your phone (Telegram): type it "
                            "below to confirm (UI-C3)"
                        )
                    return {
                        "ok": bool(r["accepted"]),
                        "reason": reason,
                        "need_confirm": name in DESTRUCTIVE and bool(r["accepted"]),
                    }
            await asyncio.sleep(0.3)
        return {
            "ok": True,
            "reason": "sent; trader-core has not answered yet (is it running?)",
            "need_confirm": name in DESTRUCTIVE,
        }

    def audit(self, days: int = 7) -> list[dict[str, Any]]:
        return self._since(
            s.commands, "ts", self.now() - timedelta(days=days), 200, {"source": "console"}
        )

    # -- UI-14 alerts, UI-15 ideas, UI-16 reports, UI-17 compliance -------------------------------
    def alerts_today(self) -> list[dict[str, Any]]:
        return self._since(s.alerts_log, "ts", day_start(self.today()), 300)

    def bot_rules(self) -> tuple[list[dict[str, Any]], str]:
        p = self.paths.alerts_db
        if p is None or not p.exists():
            return [], "alerts bot database not mounted (the bot is optional)"
        try:
            with sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2) as c:
                c.row_factory = sqlite3.Row
                rows = [dict(r) for r in c.execute("SELECT * FROM rules ORDER BY id")]
                wl = [r[0] for r in c.execute("SELECT symbol FROM watchlist ORDER BY position")]
            return rows, ("watchlist: " + ", ".join(wl) if wl else "watchlist: config default")
        except Exception as exc:  # noqa: BLE001
            return [], f"alerts bot database unreadable ({exc})"

    def idea_history(self, days: int = 20) -> list[dict[str, Any]]:
        since = self.today() - timedelta(days=days)
        try:
            return self.j.query(
                select(s.next_day_ideas)
                .where(
                    and_(s.next_day_ideas.c.plan_for >= since, s.next_day_ideas.c.status == "pick")
                )
                .order_by(desc(s.next_day_ideas.c.plan_for))
            )
        except Exception:  # noqa: BLE001
            return []

    def reports(self) -> list[dict[str, Any]]:
        root = self.paths.reports
        if not root.is_dir():
            return []
        out: list[dict[str, Any]] = [
            {
                "name": str(f.relative_to(root)),
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, UTC),
            }
            for f in root.rglob("*")
            if f.is_file() and f.suffix in (".md", ".txt", ".csv", ".json", ".html")
        ]
        return sorted(out, key=lambda r: r["mtime"], reverse=True)[:300]

    def report_path(self, name: str) -> Path | None:
        root = self.paths.reports.resolve()
        p = (root / name).resolve()
        return p if p.is_file() and root in p.parents else None  # no path traversal

    def compliance(self) -> dict[str, Any]:
        now = self.now()
        locks = [r for r in self.rows(s.locks, order_by="until", descending=True, limit=200)]
        return {
            "active": [r for r in locks if r["start"] <= now < r["until"]],
            "recent": locks[:60],
            "lots": self.rows(s.tax_lots, order_by="acquired_at", descending=True, limit=200),
            "wash": [r for r in self.rows(s.tax_lots) if r.get("disallowed_loss")],
        }

    # -- OP-18 options, UI-29..UI-32 ---------------------------------------------------------------
    def suggestions(self, days: int = 30) -> list[dict[str, Any]]:
        return self._since(s.option_suggestions, "ts", self.now() - timedelta(days=days), 300)

    def suggestion(self, sid: str) -> dict[str, Any] | None:
        r = self.rows(s.option_suggestions, {"suggestion_id": sid}, limit=1)
        return r[0] if r else None

    def notes(self, ref_id: str | None = None, days: int = 30) -> list[dict[str, Any]]:
        if ref_id:
            return self.rows(s.owner_notes, {"ref_id": ref_id}, order_by="ts")
        return self._since(s.owner_notes, "ts", self.now() - timedelta(days=days), 300)

    def add_note(
        self, ref_type: str, ref_id: str, tags: list[str], note: str, screenshot_b64: str | None
    ) -> None:
        self.j.insert(
            s.owner_notes,
            {
                "ts": self.now(),
                "ref_type": ref_type[:16],
                "ref_id": ref_id[:64],
                "tags": tags[:10],
                "note": note[:4000],
                "screenshot_b64": screenshot_b64,
            },
        )

    def closed_positions(self, days: int = 30) -> list[dict[str, Any]]:
        return [
            p
            for p in self._since(s.positions, "opened_at", self.now() - timedelta(days=days), 300)
            if p.get("closed_at") is not None
        ]

    def latency_today(self) -> list[dict[str, Any]]:
        return self._since(s.latency, "trigger_ts", day_start(self.today()), 300)

    def shadow_today(self) -> list[dict[str, Any]]:
        return self._since(s.shadow_trades, "ts", day_start(self.today()), 300)

    def copilot_tickets(self) -> list[dict[str, Any]]:
        rows = [
            d
            for d in self._since(s.decisions, "ts", day_start(self.today()), 100)
            if d["reason_code"] == "COPILOT_PLAN"
        ]
        return [{**d, **self._proposal_brief(d["proposal_id"])} for d in rows]

    # -- UI-18..UI-20 -----------------------------------------------------------------------------
    def config_view(self) -> dict[str, Any]:
        snap = self.rows(
            s.config_snapshots,
            {"effective_date": self.today()},
            order_by="id",
            descending=True,
            limit=1,
        ) or self.rows(s.config_snapshots, order_by="id", descending=True, limit=1)
        f = self.paths.config / "config.yaml"
        if not f.exists():
            f = self.paths.config / "config.example.yaml"
        return {
            "snapshot": snap[0] if snap else None,
            "file": f.read_text(encoding="utf-8") if f.exists() else "",
            "file_name": f.name,
        }

    def runs(self) -> list[dict[str, Any]]:
        root = self.paths.runs
        if not root.is_dir():
            return []
        out = []
        for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:100]:
            summ = d / "summary.json"
            data: dict[str, Any] = {}
            if summ.exists():
                try:
                    data = json.loads(summ.read_text(encoding="utf-8"))
                except ValueError:
                    data = {"error": "unreadable summary.json"}
            out.append({"name": d.name, "summary": data})
        return out

    # -- UI-8d chat history -----------------------------------------------------------------------
    def chats(self, d: date | None = None, q: str = "") -> list[dict[str, Any]]:
        rows = self.rows(s.console_chats, {"day": d or self.today()}, order_by="ts")
        if q:
            ql = q.lower()
            rows = [
                r
                for r in rows
                if ql in (r["question"] or "").lower() or ql in (r["answer"] or "").lower()
            ]
        return rows

    def save_chat(
        self, question: str, answer: str, tool: str, rows: list[dict[str, Any]], model_used: bool
    ) -> None:
        self.j.insert(
            s.console_chats,
            {
                "ts": self.now(),
                "day": self.today(),
                "question": question[:2000],
                "answer": answer[:8000],
                "tool": tool[:32],
                "rows_json": rows[:50],
                "model_used": model_used,
            },
        )

    def clear_chats(self) -> None:
        with self.j.engine.begin() as c:
            c.execute(delete(s.console_chats).where(s.console_chats.c.day == self.today()))

    def in_window(self) -> bool:
        """UI-8b: no model calls during entry windows (09:35-10:30, 15:00-15:55 ET)."""
        t = to_et(self.now()).time()
        from datetime import time as _t

        return _t(9, 35) <= t < _t(10, 30) or _t(15, 0) <= t < _t(15, 55)
