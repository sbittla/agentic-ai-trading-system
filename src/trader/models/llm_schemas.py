"""JSON schemas the Qwen agents must return (AGENT_PROMPTS.md §2-§7).

These models are passed to Ollama's ``format`` field (QW-2) and validated with Pydantic.
Fields the model must never set (ids, expiry, sizes) are not in these schemas (SEC-5);
extra keys are dropped.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _LlmOut(BaseModel):
    model_config = ConfigDict(extra="ignore")   # SEC-5: unknown fields are dropped


class NewsSummaryOut(_LlmOut):
    ticker: str
    summary: str = Field(max_length=400)
    catalyst: str = Field(max_length=160)
    tone: Literal["positive", "negative", "mixed", "neutral"]
    risk_events: list[str] = Field(default_factory=list, max_length=5)


class PlanWatchOut(_LlmOut):
    ticker: str
    setup: str
    trigger: float
    stop: float
    target_1: float
    why: str = Field(default="", max_length=200)


class OffLimitOut(_LlmOut):
    ticker: str
    reason: str = Field(max_length=160)


class GamePlanOut(_LlmOut):
    window: Literal["morning", "afternoon"]
    market_bias: Literal["with_us", "against_us", "mixed"]
    watch: list[PlanWatchOut] = Field(default_factory=list, max_length=4)
    off_limits: list[OffLimitOut] = Field(default_factory=list)


SetupLiteral = Literal["ORB", "GAP_AND_GO", "GAP_AND_GO_NO_PM", "VWAP_RECLAIM", "FIRST_PULLBACK",
                       "POWER_HOUR_BREAKOUT", "VWAP_TREND", "SWING_CLOSE_NEAR_HIGH",
                       "SWING_BASE_BREAKOUT"]


class ProposalOut(_LlmOut):
    """The part of TradeProposal the model fills (AGENT_PROMPTS §5)."""

    action: Literal["take", "skip"]
    ticker: str
    style: Literal["day", "swing"]
    window: Literal["morning", "afternoon"]
    side: Literal["buy"]
    setup: SetupLiteral
    trigger: float
    entry: float
    stop: float
    target_1: float
    target_2: float
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(max_length=300)
    weakest_point: str = Field(default="", max_length=160)


class ReviewChecksOut(_LlmOut):
    chasing: bool
    resistance_before_t1: bool
    weak_market: bool
    late_in_window: bool
    event_risk: bool
    bad_stop: bool


class ReviewOut(_LlmOut):
    proposal_id: str
    verdict: Literal["agree", "disagree"]
    main_risk: str = Field(max_length=160)
    rules_broken: list[str] = Field(default_factory=list)
    checks: ReviewChecksOut


class ReportNarrativeOut(_LlmOut):
    what_went_well: str = Field(max_length=500)
    what_to_fix: str = Field(max_length=500)
    tomorrow_focus: str = Field(max_length=500)
