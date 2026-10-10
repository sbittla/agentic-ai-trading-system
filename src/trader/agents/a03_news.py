"""A3 News and sentiment agent (A3-1..A3-4, DQ-1, DQ-4, SEC-4). Runs in news-sentiment.

Pulls company news, 8-K filings, posts and the earnings calendar; cleans every text
(untrusted: links, markup and instruction-like sentences removed, 2,000-char cap); scores
with FinBERT; asks Qwen only for a 3-sentence summary of the top items; sets the event flags.
Nothing from this agent can reach the order path: it publishes flags to the outbox only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import structlog

from trader.calendar import MacroCalendar, MarketCalendar
from trader.config import Config
from trader.interfaces import LLMClient, NewsSource
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.base import load_prompt
from trader.models.llm_schemas import NewsSummaryOut
from trader.models.messages import EventFlags, NewsItem
from trader.timeutil import et_date

log = structlog.get_logger(__name__)

URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
MD_RE = re.compile(r"[*_`#>\[\]]+")
WS_RE = re.compile(r"\s+")
INJECTION_RE = re.compile(
    r"(ignore (all |your |the )?(previous |prior |above )?(instructions|rules)|disregard .{0,30}"
    r"(instructions|rules)|you are now|system prompt|act as|new instructions|"
    r"\b(buy|sell|short)\s+\d[\d,]*\s+shares\b|override .{0,20}(risk|limit))",
    re.IGNORECASE)
RATING_RE = re.compile(r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiates? coverage|"
                       r"raises? (its )?target|cuts? (its )?target|reiterate[sd]?)\b",
                       re.IGNORECASE)
MAX_CHARS = 2000


def clean_text(text: str) -> tuple[str, bool]:
    """DQ-4 / A3-4: strip links, markup and instruction-like sentences; cap at 2,000 chars.
    Returns (cleaned, suspicious)."""
    t = TAG_RE.sub(" ", text or "")
    t = URL_RE.sub(" ", t)
    t = MD_RE.sub(" ", t)
    t = WS_RE.sub(" ", t).strip()
    sentences = re.split(r"(?<=[.!?])\s+", t)
    kept = [x for x in sentences if not INJECTION_RE.search(x)]
    suspicious = len(kept) != len(sentences)
    return " ".join(kept)[:MAX_CHARS], suspicious


class Scorer(Protocol):
    def score(self, texts: Sequence[str]) -> list[float]: ...


class LexiconScorer:
    """Small finance lexicon; used when FinBERT is not installed (dev, replay)."""

    POS = {"beat", "beats", "surge", "surges", "record", "upgrade", "upgraded", "raises",
           "strong", "growth", "profit", "bullish", "outperform", "approval", "wins", "jump",
           "rally", "expands", "partnership", "buyback"}
    NEG = {"miss", "misses", "plunge", "plunges", "downgrade", "downgraded", "cuts", "weak",
           "loss", "lawsuit", "probe", "recall", "bearish", "underperform", "falls", "drop",
           "delay", "investigation", "fine", "layoffs", "warning"}

    def score(self, texts: Sequence[str]) -> list[float]:
        out = []
        for t in texts:
            words = re.findall(r"[a-z]+", t.lower())
            p = sum(w in self.POS for w in words)
            n = sum(w in self.NEG for w in words)
            out.append(0.0 if p + n == 0 else (p - n) / (p + n))
        return out


class FinBertScorer:
    """ProsusAI/finbert on CPU (A3-2). Score = P(positive) - P(negative)."""

    def __init__(self, model: str = "ProsusAI/finbert") -> None:
        from transformers import pipeline

        self._pipe = pipeline("text-classification", model=model, top_k=None, device=-1,
                              truncation=True)

    def score(self, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        out = []
        for res in self._pipe(list(texts), batch_size=16):
            d = {r["label"].lower(): r["score"] for r in res}
            out.append(float(d.get("positive", 0) - d.get("negative", 0)))
        return out


def make_scorer() -> Scorer:
    try:
        return FinBertScorer()
    except Exception as exc:   # noqa: BLE001 - transformers missing or no weights
        log.warning("finbert_unavailable_using_lexicon", error=str(exc))
        return LexiconScorer()


@dataclass
class TickerNews:
    items: dict[str, tuple[NewsItem, str, float, bool]] = field(default_factory=dict)
    post_counts: list[int] = field(default_factory=list)   # trailing daily post counts


class NewsAgent:
    def __init__(self, cfg: Config, sources: Sequence[NewsSource], scorer: Scorer,
                 llm: LLMClient | None, journal: SqlJournal | None, calendar: MarketCalendar,
                 macro: MacroCalendar, now: Callable[[], datetime]) -> None:
        self.cfg = cfg
        self.sources = list(sources)
        self.scorer = scorer
        self.llm = llm
        self.journal = journal
        self.cal = calendar
        self.macro = macro
        self.now = now
        self.state: dict[str, TickerNews] = {t: TickerNews() for t in cfg.universe.tickers}
        self.earnings: dict[str, date] = {}
        self.last_fetch: datetime | None = None
        self.last_hot_fetch: datetime | None = None   # NW-1
        self.last_flags: dict[str, EventFlags] = {}         # NW-1: keep full-cycle summary
        self.source_errors: dict[str, str] = {}

    async def hot_cycle(self, tickers: Sequence[str]) -> list[EventFlags]:
        """NW-1: a fast poll for tickers with an open position or a game-plan setup. News and
        8-K sources only (no Reddit), FinBERT tone only (no Qwen call, so no GPU load); the
        last full cycle's summary and catalyst are kept."""
        hot = [self.cfg.canonical(t) for t in tickers if self.cfg.canonical(t) in self.state]
        if not hot:
            return []
        now = self.now()
        since = self.last_hot_fetch or self.last_fetch or (
            now - timedelta(hours=1))
        before = {t: set(self.state[t].items) for t in hot}
        items: list[NewsItem] = []
        for src in self.sources:
            if src.name == "reddit":
                continue
            try:
                items += await src.fetch(hot, since)
            except Exception as exc:   # noqa: BLE001
                self.source_errors[src.name] = str(exc)
                log.warning("news_hot_source_failed", source=src.name, error=str(exc))
        self.last_hot_fetch = now
        self.ingest(items, now)
        out: list[EventFlags] = []
        last = self.last_flags
        for t in hot:
            new_ids = set(self.state[t].items) - before[t]
            if not new_ids:
                continue
            new_scores = [self.state[t].items[i][2] for i in new_ids]
            avg_new = sum(new_scores) / len(new_scores)
            prev = last.get(t)
            tone = "negative" if avg_new <= -0.3 else "positive" if avg_new >= 0.3 else (
                prev.tone if prev else "neutral")
            recent = [sc for it, _, sc, _ in self.state[t].items.values()
                      if now - it.published_at <= timedelta(hours=24)]
            newest = max((self.state[t].items[i][0] for i in new_ids),
                         key=lambda it: it.published_at)
            out.append(EventFlags(
                ticker=t, updated_at=now, flags=tuple(self.flags_for(t, now)),
                earnings_date=self.earnings.get(t),
                summary=(prev.summary if prev else "") or clean_text(newest.headline)[0][:300],
                catalyst=prev.catalyst if prev else "none", tone=tone,
                sentiment=round(sum(recent) / len(recent), 3) if recent else 0.0,
                risk_events=prev.risk_events if prev else (),
                suspicious_items=sum(1 for *_, sus in self.state[t].items.values() if sus)))
        return out

    async def cycle(self) -> list[EventFlags]:
        """One polling cycle (7:15 then every 15 minutes, A3-1)."""
        now = self.now()
        since = self.last_fetch or (now - timedelta(hours=24))
        items: list[NewsItem] = []
        for src in self.sources:
            try:
                items += await src.fetch(list(self.cfg.universe.tickers), since)
                self.source_errors.pop(src.name, None)
            except Exception as exc:   # noqa: BLE001 - one source down must not stop the rest
                self.source_errors[src.name] = str(exc)
                log.warning("news_source_failed", source=src.name, error=str(exc))
        self.last_fetch = now
        self.ingest(items, now)
        return await self.flags_all(now)

    def ingest(self, items: Sequence[NewsItem], now: datetime) -> None:
        fresh: list[tuple[NewsItem, str, bool]] = []
        for it in items:
            t = self.cfg.canonical(it.ticker)
            if t not in self.state:
                continue
            if it.kind == "earnings":
                d = it.meta.get("date")
                if d:
                    self.earnings[t] = date.fromisoformat(str(d))
                continue
            if it.id in self.state[t].items:
                continue
            cleaned, suspicious = clean_text(f"{it.headline}. {it.text}")
            if suspicious:
                log.warning("suspicious_news_item", ticker=t, id=it.id)   # chaos test case
            fresh.append((it, cleaned, suspicious))
        scores = self.scorer.score([c for _, c, _ in fresh])
        rows = []
        for (it, cleaned, sus), sc in zip(fresh, scores, strict=True):
            t = self.cfg.canonical(it.ticker)
            self.state[t].items[it.id] = (it, cleaned, sc, sus)
            rows.append({"item_id": it.id, "ticker": t, "source": it.source,
                         "published_at": it.published_at, "received_at": now,
                         "headline": clean_text(it.headline)[0][:500], "cleaned_text": cleaned,
                         "raw_text": (it.text or "")[:20000], "finbert_score": sc,
                         "suspicious": sus})
        if self.journal is not None and rows:
            self.journal.insert_many(s.news_items, rows)

    def flags_for(self, ticker: str, now: datetime) -> list[str]:
        """A3-3 event flags."""
        d = et_date(now)
        flags: list[str] = []
        ed = self.earnings.get(ticker)
        if ed is not None:
            if ed == d:
                flags.append("EARNINGS_TODAY")
            elif d < ed and self.cal.trading_days_between(d, ed) <= 3:
                flags.append("EARNINGS_3D")
        st = self.state[ticker]
        recent = [(it, c, sc, sus) for it, c, sc, sus in st.items.values()
                  if now - it.published_at <= timedelta(hours=24)]
        if any(it.kind == "8-K" for it, *_ in recent):
            flags.append("NEW_8K")
        if any(RATING_RE.search(it.headline) or it.kind == "rating" for it, *_ in recent):
            flags.append("RATING_CHANGE")
        posts = sum(1 for it, *_ in recent if it.kind == "post")
        trailing = st.post_counts[-20:]
        if trailing and posts >= 10 and posts > 3 * (sum(trailing) / len(trailing)):
            flags.append("SOCIAL_SPIKE")
        if self.macro.is_fomc(d):
            flags.append("FOMC")
        if d in self.macro.cpi:
            flags.append("CPI")
        if d in self.macro.jobs:
            flags.append("JOBS")
        return flags

    async def summarize(self, ticker: str, now: datetime) -> NewsSummaryOut | None:
        st = self.state[ticker]
        recent = sorted([(it, c, sc) for it, c, sc, _ in st.items.values()
                         if now - it.published_at <= timedelta(hours=24)],
                        key=lambda x: -abs(x[2]))[:10]
        if not recent or self.llm is None:
            return None
        version = self.cfg.llm.prompt_versions.get("a03", "v1")
        data: dict[str, Any] = {"ticker": ticker, "items": [
            {"source": it.source, "kind": it.kind, "published_at": it.published_at.isoformat(),
             "headline": clean_text(it.headline)[0][:300], "text": c[:600],
             "score": round(sc, 3)} for it, c, sc in recent]}
        res = await self.llm.structured(agent="a03", prompt_version=version,
                                        system=load_prompt("a03", version), data=data,
                                        schema=NewsSummaryOut,
                                        timeout_s=self.cfg.llm.timeout_sec)
        return res.value if res.ok and isinstance(res.value, NewsSummaryOut) else None

    async def flags_all(self, now: datetime) -> list[EventFlags]:
        out = []
        d = et_date(now)
        for t in self.cfg.universe.tickers:
            flags = self.flags_for(t, now)
            summ = await self.summarize(t, now)
            recent = [sc for it, _, sc, _ in self.state[t].items.values()
                      if now - it.published_at <= timedelta(hours=24)]
            susp = sum(1 for it, _, _, sus in self.state[t].items.values() if sus)
            ef = EventFlags(ticker=t, updated_at=now, flags=tuple(flags),
                            earnings_date=self.earnings.get(t),
                            summary=summ.summary if summ else "",
                            catalyst=summ.catalyst if summ else "none",
                            tone=summ.tone if summ else "neutral",
                            sentiment=round(sum(recent) / len(recent), 3) if recent else 0.0,
                            risk_events=tuple(summ.risk_events) if summ else (),
                            suspicious_items=susp)
            out.append(ef)
            self.last_flags[t] = ef
            if self.journal is not None and flags:
                self.journal.insert_many(s.event_flags, [
                    {"date": d, "ticker": t, "flag": f, "detail": ef.catalyst[:200]}
                    for f in flags])
        return out

    def end_of_day(self) -> None:
        for st in self.state.values():
            st.post_counts.append(sum(1 for it, *_ in st.items.values() if it.kind == "post"))
            st.items = {}
