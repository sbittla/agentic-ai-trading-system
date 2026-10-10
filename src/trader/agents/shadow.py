"""Shadow trades (REQUIREMENTS SH-1, SH-2).

Every setup whose trigger fires is followed with a simulated bracket, whether it was traded or
not, so the weekly review can show what each rule, the A6 review and the owner's answers cost
or saved. Shadow trades never place orders and never count toward limits.

Fill model (same idea as the replay fill model, STRATEGY_SPEC §7 / BACKTEST_ENGINE §3):
- entry fills when a later 1-min bar trades at or through the limit within the cancel window
  (EX-3: 3 minutes, or price runs 0.5 ATR past the trigger first -> no fill);
- a bar that touches both stop and target is assumed to hit the stop first (conservative);
- T1: half off, the rest's stop moves to break-even (XR-2); T2 or stop closes the rest;
- day trades are closed at the window's flatten time at the bar close.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import Bar, SetupTriggered

log = structlog.get_logger(__name__)
D0 = Decimal(0)


@dataclass
class Shadow:
    setup: SetupTriggered
    proposal_id: str
    taken: bool
    reason: str
    direction: str = "long"
    status: str = "pending"            # pending -> open -> closed | nofill
    filled_at: datetime | None = None
    stop: Decimal = D0
    half_done: bool = False
    realized_r: float = 0.0
    row_id: int | None = None

    @property
    def r(self) -> Decimal:
        return self.setup.entry - self.setup.stop


@dataclass
class ShadowBook:
    journal: SqlJournal | None
    cancel_after_min: int = 3
    runaway_atr: float = 0.5
    open: list[Shadow] = field(default_factory=list)

    def add(self, s_: SetupTriggered, proposal_id: str, taken: bool, reason: str,
            direction: str = "long") -> Shadow:
        sh = Shadow(s_, proposal_id, taken, reason, direction, stop=s_.stop)
        if self.journal is not None:
            try:
                sh.row_id = self.journal.insert(s.shadow_trades, {
                    "setup_id": s_.setup_id, "proposal_id": proposal_id, "ts": s_.ts,
                    "ticker": s_.ticker, "setup": s_.setup, "window": s_.window,
                    "style": s_.style, "direction": direction, "taken": taken,
                    "reason_code": reason, "entry": s_.entry, "stop": s_.stop,
                    "t1": s_.t1, "t2": s_.t2, "status": "pending"})
            except Exception as exc:   # noqa: BLE001 - shadows never block trading
                log.warning("shadow_journal_failed", error=str(exc))
        self.open.append(sh)
        return sh

    def on_bar(self, bar: Bar, flatten_at: dict[str, datetime]) -> list[Shadow]:
        """Advance every shadow for ``bar.symbol``; returns shadows closed by this bar."""
        done: list[Shadow] = []
        for sh in list(self.open):
            if sh.setup.ticker != bar.symbol or bar.ts < sh.setup.ts:
                continue
            if sh.status == "pending":
                self._try_fill(sh, bar)
            if sh.status == "open":
                self._manage(sh, bar, flatten_at.get(sh.setup.window))
            if sh.status in ("closed", "nofill"):
                self.open.remove(sh)
                if sh.status == "nofill":
                    self._persist(sh)
                done.append(sh)
        return done

    def _try_fill(self, sh: Shadow, bar: Bar) -> None:
        s_ = sh.setup
        if bar.ts >= s_.ts + timedelta(minutes=self.cancel_after_min):
            sh.status = "nofill"
            return
        runaway = s_.trigger + Decimal(str(self.runaway_atr * s_.atr5))
        if Decimal(str(bar.l)) <= s_.entry:
            sh.status, sh.filled_at = "open", bar.ts
        elif Decimal(str(bar.h)) >= runaway:
            sh.status = "nofill"

    def _manage(self, sh: Shadow, bar: Bar, flatten: datetime | None) -> None:
        s_, r = sh.setup, sh.r
        if r <= 0:
            sh.status = "nofill"
            return
        lo, hi = Decimal(str(bar.l)), Decimal(str(bar.h))
        rest = 0.5 if sh.half_done else 1.0
        if lo <= sh.stop:                       # stop first when both are touched
            px = min(sh.stop, Decimal(str(bar.o)))   # a gap through the stop fills worse
            sh.realized_r += rest * float((px - s_.entry) / r)
            self._close(sh, bar.end, px, "STOP" if not sh.half_done else "BREAKEVEN")
            return
        if not sh.half_done and hi >= s_.t1:
            sh.half_done = True
            sh.realized_r += 0.5 * float((s_.t1 - s_.entry) / r)
            sh.stop = max(sh.stop, s_.entry)
            rest = 0.5
        if sh.half_done and hi >= s_.t2:
            sh.realized_r += rest * float((s_.t2 - s_.entry) / r)
            self._close(sh, bar.end, s_.t2, "T2")
            return
        if s_.style == "day" and flatten is not None and bar.end >= flatten:
            px = Decimal(str(bar.c))
            sh.realized_r += rest * float((px - s_.entry) / r)
            self._close(sh, bar.end, px, "TIME")

    def _close(self, sh: Shadow, ts: datetime, px: Decimal, why: str) -> None:
        sh.status = "closed"
        self._persist(sh, ts, px, why)

    def _persist(self, sh: Shadow, ts: datetime | None = None, px: Decimal | None = None,
                 why: str | None = None) -> None:
        if self.journal is None or sh.row_id is None:
            return
        vals: dict[str, Any] = {"status": sh.status, "filled_at": sh.filled_at}
        if sh.status == "closed":
            vals.update({"closed_at": ts, "exit_price": px, "exit_reason": why,
                         "r_multiple": round(sh.realized_r, 4)})
        try:
            self.journal.update(s.shadow_trades, {"id": sh.row_id}, vals)
        except Exception as exc:   # noqa: BLE001
            log.warning("shadow_update_failed", error=str(exc))

    def expire_all(self) -> None:
        """End of session: unfilled day shadows lapse; open day shadows were closed at the
        flatten time; swing shadows are dropped (graded on day trades only, v1)."""
        for sh in list(self.open):
            sh.status = "nofill" if sh.status == "pending" else "closed"
            self._persist(sh)
        self.open.clear()


def shadow_report(rows: list[dict[str, Any]], flag_after: int = 40) -> tuple[str, list[str]]:
    """SH-2: expectancy of taken vs not-taken setups by reason. Returns (text, flags): a rule
    whose blocked setups made money over ``flag_after``+ shadows is flagged for review."""
    closed = [r for r in rows if r.get("status") == "closed" and r.get("r_multiple") is not None]
    if not closed:
        return "Shadow trades (SH-2): none closed yet", []
    taken = [float(r["r_multiple"]) for r in closed if r.get("taken")]
    by_reason: dict[str, list[float]] = defaultdict(list)
    for r in closed:
        if not r.get("taken"):
            by_reason[str(r.get("reason_code") or "?")].append(float(r["r_multiple"]))

    def exp(v: list[float]) -> str:
        return f"{sum(v) / len(v):+.2f}R over {len(v)}" if v else "none"

    lines = [f"Shadow trades (SH-2): taken {exp(taken)}"]
    flags: list[str] = []
    for reason, v in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        e = sum(v) / len(v)
        verdict = "cost money" if e > 0 else "saved money"
        lines.append(f"  not taken · {reason}: {exp(v)} -> the rule {verdict}")
        if len(v) >= flag_after and e > 0:
            flags.append(f"{reason}: blocked setups averaged {e:+.2f}R over {len(v)} shadows "
                         "(review the rule; it never changes on its own)")
    return "\n".join(lines), flags
