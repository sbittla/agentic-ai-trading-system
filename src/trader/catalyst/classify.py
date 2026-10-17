"""Catalyst type and direction (NC-5, NC-6).

Rules first (headline patterns and 8-K item numbers); Qwen only for stories no rule types,
with the cleaned headlines in a DATA block and the answer limited to the enum (SEC-5).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from trader.catalyst.stories import Story, source_weight
from trader.interfaces import LLMClient
from trader.llm.base import load_prompt
from trader.models.llm_schemas import CatalystTypeOut

CatalystType = Literal["EARNINGS", "GUIDANCE", "ANALYST", "M_AND_A", "PRODUCT",
                       "REGULATORY_LEGAL", "MANAGEMENT", "CAPITAL", "INSIDER", "MACRO_SECTOR",
                       "SOCIAL_ONLY", "OTHER"]
TYPES: tuple[str, ...] = ("EARNINGS", "GUIDANCE", "ANALYST", "M_AND_A", "PRODUCT",
                          "REGULATORY_LEGAL", "MANAGEMENT", "CAPITAL", "INSIDER", "MACRO_SECTOR",
                          "SOCIAL_ONLY", "OTHER")

# order matters: the first match wins (guidance in an earnings release is EARNINGS)
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EARNINGS", re.compile(
        r"\b(earnings|eps\b|quarterly (results|profit|revenue)|q[1-4] (results|earnings|revenue|"
        r"profit)|(beats?|miss(es)?|tops?) (estimates|expectations|forecasts)|"
        r"(first|second|third|fourth)[- ]quarter (results|profit|revenue|earnings))", re.I)),
    ("GUIDANCE", re.compile(
        r"\b((raises?|lifts?|boosts?|cuts?|lowers?|slashes|reaffirms?|withdraws?) "
        r"(its |full[- ]year |annual |\w+ )?(guidance|outlook|forecast)|"
        r"(guidance|outlook|forecast) (raised|cut|lowered|above|below))", re.I)),
    ("ANALYST", re.compile(
        r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiates? coverage|"
        r"(raises?|cuts?|lowers?) (its |the )?(price )?target|reiterate[sd]? (buy|sell|hold|"
        r"overweight|underweight|outperform))", re.I)),
    ("M_AND_A", re.compile(
        r"\b(acquires?|acquisition|to acquire|merger|merge[sd]?|takeover|buyout|"
        r"(agrees?|deal|bid|offer) to (buy|acquire)|tender offer)\b", re.I)),
    ("MANAGEMENT", re.compile(
        r"\b(ceo|cfo|coo|chief (executive|financial|operating) officer|chairman)\b.{0,60}"
        r"\b(resigns?|steps? down|departs?|retires?|fired|ousted|appoint(s|ed)?|named|"
        r"to succeed|successor)\b", re.I)),
    ("CAPITAL", re.compile(
        r"\b(buyback|share repurchase|repurchase program|dividend|stock split|"
        r"(secondary|share|stock|equity|debt|notes|bond|convertible) offering|"
        r"prices? \$?[\d.]+ ?(billion|million) (of )?(notes|bonds))\b", re.I)),
    ("REGULATORY_LEGAL", re.compile(
        r"\b(fda|antitrust|doj|ftc|sec (probe|charges|investigation)|lawsuit|sued|sues|"
        r"class action|investigation|probe|fined|penalty|settle(s|d|ment)?|regulators?|"
        r"court|ruling|verdict|recall)\b", re.I)),
    ("MACRO_SECTOR", re.compile(
        r"\b(tariffs?|export (ban|controls?|restrictions?|curbs?)|sanctions|chip stocks|"
        r"semiconductor stocks|tech stocks|sector|fed\b|rate (cut|hike)s?|inflation|"
        r"jobs report|cpi)\b", re.I)),
    ("PRODUCT", re.compile(
        r"\b(launch(es|ed)?|unveil(s|ed)?|introduc(es|ed)|releases?|contract|orders?\b|"
        r"partnership|partners? with|deal with|wins?|awarded|approval|approves?)\b", re.I)),
)
# 8-K item numbers (SEC Form 8-K General Instructions)
ITEM_TYPES = {"2.02": "EARNINGS", "1.01": "M_AND_A", "2.01": "M_AND_A", "5.02": "MANAGEMENT",
              "3.02": "CAPITAL", "8.01": "OTHER", "7.01": "OTHER", "1.05": "REGULATORY_LEGAL"}
ITEM_RE = re.compile(r"\b(\d\.\d\d)\b")
# NC-48: previews, opinion and listicles describe no new event (the event, if any, comes later)
NON_EVENT_RE = re.compile(
    r"(\bto report\b|\bahead of\b.{0,50}\b(earnings|results|report)|\bpreview\b|"
    r"here'?s what|\bwhat .{0,40} says comes next\b|"
    r"what to (expect|watch)|what'?s in the cards|\bshould you\b|\bis it (time|too late)\b|"
    r"\b(is|are) \w+( \w+)? (a|an) (buy|sell)\b|stocks? to (buy|sell|watch)|"
    r"\bbetter buy\b|\bprobably\b|\bcould (reach|soar|double|triple|be)\b|"
    r"\bmight\b|\bnet worth\b|\bmillionaire\b|\bif you('d| had)? invested\b|"
    r"\b(top|best) \d+ |\d+ (reasons|things|stocks)\b|\bexpected to\b|\bmay\b|"
    r"\bnext week\b|\binto (earnings|the report|results)\b|\bwhere to buy\b|"
    r"\bwarns? (\w+ )?(bulls|bears|investors)\b)", re.I)
POS_RE = re.compile(r"\b(beats?|tops?|raises?|raised|lifts?|record|surges?|soars?|jumps?|"
                    r"upgrade[sd]?|wins?|awarded|approv(al|ed|es)|buyback|outperform|strong)\b",
                    re.I)
NEG_RE = re.compile(r"\b(miss(es)?|cuts?|lowers?|slashes|plunges?|sinks?|falls?|tumbles?|"
                    r"downgrade[sd]?|probe|lawsuit|sued|recall|resigns?|warns?|weak|delays?|"
                    r"halts?|withdraws?|fined|loss)\b", re.I)


def classify_rules(story: Story) -> tuple[str, str] | None:
    """(type, rule) from the story's headlines and 8-K items, or None."""
    if story.kinds <= {"post"}:
        return "SOCIAL_ONLY", "kind:post"
    if "form4" in story.kinds:
        return "INSIDER", "kind:form4"
    if "rating" in story.kinds:
        return "ANALYST", "kind:rating"
    heads = " | ".join(i.headline for i in story.items[:12])
    lead = story.items[0].headline
    if NON_EVENT_RE.search(lead) and not ({"8-K", "rating"} & story.kinds):
        return "OTHER", "re:non_event"
    for ctype, rx in RULES:
        if rx.search(heads):
            return ctype, f"re:{ctype.lower()}"
    for it in story.items:
        if it.kind == "8-K":
            for item in ITEM_RE.findall(it.headline):
                if item in ITEM_TYPES and ITEM_TYPES[item] != "OTHER":
                    return ITEM_TYPES[item], f"8-K:{item}"
    return None


def direction(story: Story, weights: Mapping[str, float], default_w: float) -> tuple[str, float]:
    """(positive | negative | mixed, weighted sentiment -1..+1). FinBERT per item, weighted by
    source; headline words break ties when FinBERT is near zero."""
    num = den = 0.0
    for it in story.items:
        w = max(source_weight(it.publisher or it.source, weights, default_w), 0.2)
        words = len(POS_RE.findall(it.headline)) - len(NEG_RE.findall(it.headline))
        s = it.sentiment if abs(it.sentiment) >= 0.1 else 0.3 * max(-1, min(1, words))
        num += w * s
        den += w
    sent = num / den if den else 0.0
    d = "positive" if sent > 0.15 else "negative" if sent < -0.15 else "mixed"
    return d, round(sent, 3)


async def classify_llm(llm: LLMClient, story: Story, version: str = "v1"
                       ) -> CatalystTypeOut | None:
    """NC-5: Qwen types a story no rule matched. Headlines are cleaned text (DQ-4)."""
    from trader.agents.a03_news import clean_text

    data: dict[str, Any] = {"ticker": story.ticker, "headlines": [
        clean_text(i.headline)[0][:240] for i in story.items[:8]]}
    res = await llm.structured(agent="catalyst", prompt_version=version,
                               system=load_prompt("catalyst", version), data=data,
                               schema=CatalystTypeOut)
    out = res.value if res.ok else None
    return out if isinstance(out, CatalystTypeOut) else None
