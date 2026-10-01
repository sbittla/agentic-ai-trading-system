"""A6 Review agent (Qwen): argues against each proposal (A6-1..A6-3).

Separate prompt, temperature 0. Returns agree/disagree, the main risk and any rule broken.
No valid review means no trade (QW-3). A disagreement drops the trade in auto mode or goes to
the owner in approve mode (handled by A7).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from trader.config import Config
from trader.interfaces import LLMClient
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.base import load_prompt
from trader.models.llm_schemas import ReviewOut
from trader.models.messages import Review, ReviewChecks, TradeProposal

RULES = [
    "CK-6 never chase: entry no more than 0.25 ATR(5-min) above the trigger",
    "CK-7 stop at structure, no wider than 1 ATR(5-min) for day trades",
    "CK-8 nearest resistance at least 1.5R above entry; target_2 at least 2R",
    "MF-1 QQQ above VWAP and not down more than 1% on the day",
    "MF-2 for NVDA, AVGO, TSM the SMH ETF must be above VWAP",
    "MF-3 relative volume at least 1.5 for the time of day",
    "DR-1 never average down; DR-3 no revenge trades",
    "XR-6 no swing position held into an earnings release",
]


class ReviewAgent:
    def __init__(self, cfg: Config, llm: LLMClient, journal: SqlJournal | None,
                 now: Callable[[], datetime]) -> None:
        self.cfg = cfg
        self.llm = llm
        self.journal = journal
        self.now = now

    async def review(self, p: TradeProposal, data: dict[str, Any]) -> tuple[Review | None,
                                                                            str | None]:
        version = self.cfg.llm.prompt_versions.get("a06", "v1")
        payload = {**data, "proposal": p.model_dump(mode="json", exclude={"model"}),
                   "rules": RULES}
        res = await self.llm.structured(agent="a06", prompt_version=version,
                                        system=load_prompt("a06", version), data=payload,
                                        schema=ReviewOut, timeout_s=self.cfg.llm.timeout_sec)
        if not res.ok or not isinstance(res.value, ReviewOut):
            return None, res.error or "invalid review"
        out = res.value
        if out.proposal_id != p.proposal_id:
            return None, "review proposal_id does not match"
        rv = Review(proposal_id=p.proposal_id, verdict=out.verdict, main_risk=out.main_risk,
                    rules_broken=tuple(out.rules_broken),
                    checks=ReviewChecks(**out.checks.model_dump()))
        if self.journal is not None:
            self.journal.upsert(s.reviews, {"proposal_id": p.proposal_id}, {
                "ts": self.now(), "verdict": rv.verdict, "main_risk": rv.main_risk,
                "rules_broken": list(rv.rules_broken), "json": rv.model_dump(mode="json")})
        return rv, None
