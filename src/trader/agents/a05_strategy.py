"""A5 Strategy agent (Qwen): game plans and trade proposals (A5-1..A5-4, PR-1..PR-6).

Qwen proposes; code validates every number (PR-4) and sets ids, expiry and model fields.
A proposal that fails schema (after one retry) or code validation is dropped: no trade.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import structlog

from trader.config import Config
from trader.interfaces import LLMClient
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.base import load_prompt
from trader.models.llm_schemas import GamePlanOut, ProposalOut
from trader.models.messages import (
    AFTERNOON_SETUPS,
    MORNING_SETUPS,
    GamePlan,
    GamePlanItem,
    SetupTriggered,
    TradeProposal,
)
from trader.timeutil import et_datetime, to_et

log = structlog.get_logger(__name__)
PREVIEW_MIN_SCORE = 70   # session_screen readiness needed to fill a preview plan slot
NAMESPACE = uuid.UUID("6f1c2a47-6e1b-4d0e-9a0b-2c7d3f9e8a11")


@dataclass
class ProposalOutcome:
    proposal: TradeProposal | None
    error: str | None
    raw: str = ""
    latency_ms: int = 0


def proposal_id_for(setup_id: str) -> str:
    """Deterministic, unique per setup: replays reproduce ids (RP-2), duplicates are caught by
    A8-2 because the same setup always maps to the same client order ID."""
    return str(uuid.uuid5(NAMESPACE, setup_id))


def numbers_in(obj: Any) -> list[float]:
    out: list[float] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += numbers_in(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            out += numbers_in(v)
    elif isinstance(obj, int | float | Decimal) and not isinstance(obj, bool):
        out.append(float(obj))
    return out


class StrategyAgent:
    def __init__(self, cfg: Config, llm: LLMClient, journal: SqlJournal | None,
                 now: Callable[[], datetime]) -> None:
        self.cfg = cfg
        self.llm = llm
        self.journal = journal
        self.now = now
        self.plans: dict[tuple[date, str], GamePlan] = {}

    # -- A5-1 game plan ----------------------------------------------------------------------
    async def game_plan(self, d: date, window: str, data: dict[str, Any],
                        locks: list[dict[str, Any]], preview: bool = False) -> GamePlan:
        """``preview``: the plan for session ``d`` built after the previous close from the
        latest completed session (no gap or pre-market yet). The 9:00 / 2:45 plans replace it."""
        agent = "a05_preview" if preview else "a05_plan"
        version = self.cfg.llm.prompt_versions.get(agent, "v1")
        setups = MORNING_SETUPS if window == "morning" else AFTERNOON_SETUPS
        system = load_prompt(agent, version).replace("{window}", window).replace(
            "{setup_list}", ", ".join(setups)).replace("{plan_for}", d.isoformat())
        payload = {**data, "window": window, "locks": locks}
        off = [(x["ticker"], x.get("reason", "locked")) for x in locks]
        if preview and not data.get("tickers"):   # no candidate for this window: no LLM call
            return self._store(d, window, GamePlan(
                day=d, window="morning" if window == "morning" else "afternoon",
                off_limits=tuple(off), source="preview"))
        res = await self.llm.structured(agent=agent, prompt_version=version,
                                        system=system, data=payload, schema=GamePlanOut,
                                        timeout_s=self.cfg.llm.timeout_sec)
        if not res.ok or not isinstance(res.value, GamePlanOut):
            if (d, window) in self.plans:   # failed retry or preview: keep the journaled plan
                return self.plans[(d, window)]
            # preview: Qwen down still leaves the code screen's picks (no veto available)
            watch = tuple(self._screen_fill(data, [], {t for t, _ in off}, setups)) \
                if preview else ()
            plan = GamePlan(day=d, window="morning" if window == "morning" else "afternoon",
                            watch=watch, off_limits=tuple(off),
                            source="preview" if preview else "fallback")
        else:
            out = res.value
            nums = numbers_in(data)
            locked = {x["ticker"] for x in locks}
            items: list[GamePlanItem] = []
            for w in out.watch[:4]:
                if (w.ticker not in self.cfg.universe.tickers or w.ticker in locked
                        or w.setup not in setups):
                    continue
                if not all(_close_to_any(v, nums) for v in (w.trigger, w.stop, w.target_1)):
                    log.info("plan_item_dropped_numbers", ticker=w.ticker)   # PR-4
                    continue
                items.append(GamePlanItem(ticker=w.ticker, setup=w.setup, trigger=w.trigger,
                                          stop=w.stop, target_1=w.target_1, why=w.why))
            for o in out.off_limits:
                if o.ticker in self.cfg.universe.tickers and o.ticker not in {t for t, _ in off}:
                    off.append((o.ticker, o.reason))
            if preview:
                items += self._screen_fill(data, items, {t for t, _ in off}, setups)
            plan = GamePlan(day=d, window="morning" if window == "morning" else "afternoon",
                            market_bias=out.market_bias, watch=tuple(items),
                            off_limits=tuple(off), source="preview" if preview else "llm")
        return self._store(d, window, plan)

    def _store(self, d: date, window: str, plan: GamePlan) -> GamePlan:
        self.plans[(d, window)] = plan
        if self.journal is not None:
            rows: list[dict[str, Any]] = [
                {"date": d, "window": window, "ticker": i.ticker, "setup": i.setup,
                     "trigger": i.trigger, "stop": i.stop, "target": i.target_1,
                     "notes": i.why, "off_limits": [list(x) for x in plan.off_limits],
                     "source": plan.source} for i in plan.watch]
            if not rows:
                rows = [{"date": d, "window": window, "ticker": None, "setup": None,
                         "notes": "no stocks picked", "off_limits": [list(x) for x in
                                                                     plan.off_limits],
                         "source": plan.source}]
            self.journal.insert_many(s.game_plans, rows)
        return plan

    def _screen_fill(self, data: dict[str, Any], items: list[GamePlanItem], excluded: set[str],
                     setups: tuple[str, ...]) -> list[GamePlanItem]:
        """Preview plans: fill open slots (max 4) from the code screen, best readiness first.
        The model's picks come first and its off-limits list is a veto. A plan item only marks
        a later live trigger as planned (full size, DR-7); every entry still needs the live
        trigger, the checklist, A6 review and A7 risk."""
        have = {i.ticker for i in items}
        ranked = sorted(((t, v.get("session") or {}) for t, v in data.get("tickers", {}).items()),
                        key=lambda x: -int(x[1].get("readiness_score", 0)))
        out: list[GamePlanItem] = []
        for t, scr in ranked:
            if len(items) + len(out) >= 4:
                break
            score = int(scr.get("readiness_score", 0))
            if (score < PREVIEW_MIN_SCORE or t in have or t in excluded
                    or t not in self.cfg.universe.tickers):
                continue
            c = next((c for c in scr.get("candidates", []) if c.get("setup") in setups), None)
            if c is None:
                continue
            out.append(GamePlanItem(ticker=t, setup=c["setup"], trigger=c["trigger"],
                                    stop=c["stop"], target_1=c["target_1"],
                                    why=f"screen {score}/100: {c.get('basis', '')}"[:200]))
        return out

    def planned(self, ticker: str, setup: str, d: date, window: str) -> bool:
        """CK-4 / DR-7: was this ticker+setup on today's plan for the window?"""
        base = setup.replace("_NO_PM", "")
        plan = self.plans.get((d, window))
        return plan is not None and any(i.ticker == ticker and i.setup.replace("_NO_PM", "")
                                        == base for i in plan.watch)

    # -- A5-2 proposals ----------------------------------------------------------------------
    def expiry(self, setup: SetupTriggered, now: datetime) -> datetime:
        """A5-4: 2 minutes for day trades; 15:50 for swing trades."""
        if setup.style == "swing":
            return et_datetime(to_et(now).date(), time(15, 50))
        return now + timedelta(minutes=2)

    async def propose(self, setup: SetupTriggered, data: dict[str, Any], planned: bool,
                      live_price: float) -> ProposalOutcome:
        version = self.cfg.llm.prompt_versions.get("a05_proposal", "v1")
        system = load_prompt("a05_proposal", version)
        res = await self.llm.structured(agent="a05_proposal", prompt_version=version,
                                        system=system, data=data, schema=ProposalOut,
                                        timeout_s=self.cfg.llm.timeout_sec)
        now = self.now()
        pid = proposal_id_for(setup.setup_id)
        if not res.ok or not isinstance(res.value, ProposalOut):
            self._journal(pid, setup, None, False, res.error, version, res.latency_ms,
                          res.input_tokens + res.output_tokens)
            return ProposalOutcome(None, res.error or "invalid", res.raw, res.latency_ms)
        out = res.value
        prop = TradeProposal(
            proposal_id=pid, setup_id=setup.setup_id, action=out.action, ticker=out.ticker,
            style=out.style, window=out.window, setup=out.setup, planned=planned,
            trigger=out.trigger, entry=out.entry, stop=out.stop, target_1=out.target_1,
            target_2=out.target_2, confidence=out.confidence, thesis=out.thesis,
            weakest_point=out.weakest_point, expires_at=self.expiry(setup, now), created_at=now,
            model=getattr(self.llm, "model", "?"), prompt_version=version)
        err = self.validate(prop, setup, live_price) if prop.action == "take" else None
        self._journal(pid, setup, prop, err is None, err, version, res.latency_ms,
                      res.input_tokens + res.output_tokens)
        return ProposalOutcome(prop if err is None else None, err, res.raw, res.latency_ms)

    def validate(self, p: TradeProposal, setup: SetupTriggered, live: float) -> str | None:
        """AGENT_PROMPTS §5 code-side validation + PR-4 (numbers from input only)."""
        cfg = self.cfg
        if p.ticker != setup.ticker or p.ticker not in cfg.universe.tickers:
            return f"ticker {p.ticker} does not match setup/list (UR-1)"
        if p.setup.replace("_NO_PM", "") != setup.setup.replace("_NO_PM", "") or \
                p.style != setup.style or p.window != setup.window:
            return "setup/style/window differ from the trigger"
        if not (p.stop < p.trigger <= p.entry < p.target_1 < p.target_2):
            return "price order must be stop < trigger <= entry < target_1 < target_2"
        for name, v in (("trigger", p.trigger), ("entry", p.entry), ("stop", p.stop),
                        ("target_1", p.target_1), ("target_2", p.target_2)):
            if live > 0 and abs(v - live) / live > 0.01 and name in ("trigger", "entry"):
                return f"{name} {v} more than 1% from the live quote {live}"
        mine = {"trigger": float(setup.trigger), "entry": float(setup.entry),
                "stop": float(setup.stop), "target_1": float(setup.t1),
                "target_2": float(setup.t2)}
        for k, v in mine.items():
            if abs(getattr(p, k) - v) > 0.011:
                return f"{k} {getattr(p, k)} is not the number in DATA ({v}) (PR-4)"
        if (p.target_2 - p.entry) / (p.entry - p.stop) < cfg.entries.min_reward_risk - 1e-9:
            return f"reward:risk below {cfg.entries.min_reward_risk}"
        if p.entry - p.trigger > cfg.entries.max_chase_atr5 * setup.atr5 + 0.011:
            return "entry more than 0.25 ATR(5-min) above trigger (CK-6)"
        if p.confidence < cfg.entries.min_confidence:
            return f"confidence {p.confidence} < {cfg.entries.min_confidence}"
        return None

    def _journal(self, pid: str, setup: SetupTriggered, prop: TradeProposal | None, valid: bool,
                 err: str | None, version: str, latency: int, tokens: int) -> None:
        if self.journal is None:
            return
        self.journal.upsert(s.proposals, {"proposal_id": pid}, {
            "setup_id": setup.setup_id, "ts": self.now(), "ticker": setup.ticker,
            "json": prop.model_dump(mode="json") if prop else None, "valid": valid,
            "error": err, "model": getattr(self.llm, "model", "?"), "prompt_version": version,
            "latency_ms": latency, "tokens": tokens})


def _close_to_any(v: float, nums: list[float], tol: float = 0.005) -> bool:
    return any(abs(v - n) <= max(0.011, abs(n) * tol) for n in nums)
