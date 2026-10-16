"""Optional Qwen summary of a scan (TS-13). The model only words what the code computed:
any price in its text that is not in the input drops the summary."""

from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel, Field

from trader.llm.base import load_prompt
from trader.scanner.inplay import InPlay
from trader.scanner.techniques import Idea

PROMPT_VERSION = "v1"
NUM_RE = re.compile(r"\d+\.\d+")


class ScanSummary(BaseModel):
    summary: str = Field(max_length=900)


class _LLM(Protocol):
    async def structured(self, *, agent: str, prompt_version: str, system: str,
                         data: dict[str, Any], schema: type[BaseModel],
                         timeout_s: float = 60) -> Any: ...


def _payload(ideas: list[Idea], plays: list[InPlay]) -> dict[str, Any]:
    return {"swing": [{k: d.as_dict()[k] for k in ("symbol", "technique", "side", "trigger",
                                                   "stop", "t1", "t2", "score", "why",
                                                   "confluence")} for d in ideas],
            "day_watchlist": [{"symbol": p.symbol, "expected_range_pct": p.expected_range_pct,
                               "bias": p.bias, **p.levels} for p in plays]}


def numbers_ok(text: str, data: dict[str, Any]) -> bool:
    """Every decimal number in ``text`` must appear in ``data`` (TS-13)."""
    allowed: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, float):
            allowed.update({f"{v:.2f}", f"{v:.1f}", f"{v:g}"})
        elif isinstance(v, str):
            allowed.update(NUM_RE.findall(v))
    walk(data)
    return all(n in allowed for n in NUM_RE.findall(text))


async def explain(llm: _LLM, ideas: list[Idea], plays: list[InPlay]) -> str | None:
    data = _payload(ideas, plays)
    if not data["swing"] and not data["day_watchlist"]:
        return None
    res = await llm.structured(agent="scan", prompt_version=PROMPT_VERSION,
                               system=load_prompt("scan", PROMPT_VERSION), data=data,
                               schema=ScanSummary, timeout_s=90)
    if not getattr(res, "ok", False) or res.value is None:
        return None
    text = str(res.value.summary).strip()
    return text if text and numbers_ok(text, data) else None
