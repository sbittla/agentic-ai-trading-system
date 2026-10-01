"""LLM stand-ins for backtests and tests.

``RulesLLM``: Backtest-rules mode (BACKTEST_ENGINE §1) - A5/A6 replaced by "take every valid
setup", using only numbers from DATA. ``ScriptedLLM``: returns scripted raw outputs per agent,
for schema/retry/injection tests.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from trader.interfaces import LLMResult
from trader.llm.base import extract_json
from trader.models.llm_schemas import (
    GamePlanOut,
    NewsSummaryOut,
    ProposalOut,
    ReportNarrativeOut,
    ReviewOut,
)


class RulesLLM:
    model = "rules-v1"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def structured(self, *, agent: str, prompt_version: str, system: str,
                         data: dict[str, Any], schema: type[BaseModel],
                         timeout_s: float = 60) -> LLMResult:
        self.calls.append(agent)
        value: BaseModel
        if schema is ProposalOut:
            st = data["setup"]
            value = ProposalOut(action="take", ticker=st["ticker"], style=st["style"],
                                window=st["window"], side="buy", setup=st["setup"],
                                trigger=st["trigger"], entry=st["entry"], stop=st["stop"],
                                target_1=st["t1"], target_2=st["t2"], confidence=0.7,
                                thesis=f"{st['setup']} triggered on a closed bar.",
                                weakest_point="rules-only backtest")
        elif schema is ReviewOut:
            value = ReviewOut.model_validate({
                "proposal_id": data["proposal"]["proposal_id"], "verdict": "agree",
                "main_risk": "none found by rules", "rules_broken": [],
                "checks": {k: False for k in ("chasing", "resistance_before_t1", "weak_market",
                                              "late_in_window", "event_risk", "bad_stop")}})
        elif schema is GamePlanOut:
            value = GamePlanOut(window=data.get("window", "morning"), market_bias="mixed",
                                watch=[], off_limits=[{"ticker": x["ticker"],   # type: ignore
                                                       "reason": x.get("reason", "locked")}
                                                      for x in data.get("locks", [])])
        elif schema is NewsSummaryOut:
            items = data.get("items", [])
            top = max(items, key=lambda x: abs(x.get("score", 0)), default=None)
            cat = top["headline"][:150] if top and abs(top.get("score", 0)) >= 0.3 else "none"
            avg = sum(x.get("score", 0) for x in items) / len(items) if items else 0.0
            tone: Literal["positive", "negative", "neutral"] = (
                "positive" if avg > 0.2 else "negative" if avg < -0.2 else "neutral")
            value = NewsSummaryOut(ticker=data.get("ticker", ""), summary="; ".join(
                x["headline"] for x in items[:3])[:390], catalyst=cat, tone=tone,
                risk_events=[])
        elif schema is ReportNarrativeOut:
            value = ReportNarrativeOut(what_went_well="Rules were followed on every decision.",
                                       what_to_fix="Review the most common rejection reason.",
                                       tomorrow_focus="Trade only the game plan setups.")
        else:
            return LLMResult(ok=False, error=f"RulesLLM has no rule for {schema.__name__}")
        return LLMResult(ok=True, value=value, raw=value.model_dump_json(), model=self.model)

    async def healthy(self) -> bool:
        return True


class ScriptedLLM:
    """Returns queued raw strings per agent; validates them like the real client."""

    model = "scripted"

    def __init__(self, scripts: dict[str, list[str]] | None = None, retries: int = 1,
                 fallback: RulesLLM | None = None) -> None:
        self.scripts: dict[str, deque[str]] = defaultdict(deque)
        for k, v in (scripts or {}).items():
            self.scripts[k].extend(v)
        self.retries = retries
        self.fallback = fallback
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.up = True

    async def structured(self, *, agent: str, prompt_version: str, system: str,
                         data: dict[str, Any], schema: type[BaseModel],
                         timeout_s: float = 60) -> LLMResult:
        self.calls.append((agent, data))
        if not self.up:
            return LLMResult(ok=False, error="LLMUnavailable: down")
        if not self.scripts[agent]:
            if self.fallback is not None:
                return await self.fallback.structured(agent=agent, prompt_version=prompt_version,
                                                      system=system, data=data, schema=schema)
            return LLMResult(ok=False, error="no script")
        err = None
        for _ in range(self.retries + 1):
            if not self.scripts[agent]:
                break
            raw = self.scripts[agent].popleft()
            try:
                return LLMResult(ok=True, value=schema.model_validate_json(extract_json(raw)),
                                 raw=raw, model=self.model)
            except (ValidationError, ValueError) as exc:
                err = f"LLMInvalidOutput: {exc}"
        return LLMResult(ok=False, error=err or "exhausted")

    async def healthy(self) -> bool:
        return self.up
