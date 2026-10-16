"""Stories: news items about the same event grouped together (NC-1..NC-4).

An item joins the open story it is most similar to (same ticker, within the story window),
or opens a new one. Similarity is the sentence-embedding cosine when both items have one
(news-sentiment stores them), else the word-hash cosine, each with its own threshold.
Deterministic: items are processed in (published_at, item_id) order.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trader.catalyst.text import HashingEmbedder, Vec, cosine


@dataclass
class Item:
    item_id: str
    ticker: str
    published_at: datetime
    headline: str
    text: str = ""
    source: str = "finnhub"        # feed: finnhub | edgar | rss | reddit
    kind: str = "news"             # news | 8-K | rating | post | form4
    publisher: str = ""
    sentiment: float = 0.0         # FinBERT P(pos) - P(neg)
    vec: Vec | None = None         # sentence embedding (news-sentiment), if stored
    hvec: Vec | None = None        # word-hash vector, always computed here


@dataclass
class Story:
    story_id: str
    ticker: str
    items: list[Item] = field(default_factory=list)
    novelty: float = 1.0

    @property
    def first_seen(self) -> datetime:
        return self.items[0].published_at

    @property
    def last_seen(self) -> datetime:
        return self.items[-1].published_at

    @property
    def headline(self) -> str:
        return self.items[0].headline

    @property
    def publishers(self) -> set[str]:
        return {(i.publisher or i.source).lower() for i in self.items}

    @property
    def kinds(self) -> set[str]:
        return {i.kind for i in self.items}


def similarity(a: Item, b: Item) -> tuple[float, bool]:
    """(cosine, used sentence embeddings)."""
    if a.vec is not None and b.vec is not None and a.vec.shape == b.vec.shape:
        return cosine(a.vec, b.vec), True
    if a.hvec is None or b.hvec is None:
        return 0.0, False
    return cosine(a.hvec, b.hvec), False


def _joins(a: Item, b: Item, join: float, join_hash: float) -> float:
    sim, model = similarity(a, b)
    return sim if sim >= (join if model else join_hash) else -1.0


def story_id(first: Item) -> str:
    return "st-" + hashlib.sha1(f"{first.ticker}|{first.item_id}".encode(),
                                usedforsecurity=False).hexdigest()[:20]


def cluster(items: Iterable[Item], *, join: float = 0.75, join_hash: float = 0.45,
            window_h: int = 72) -> list[Story]:
    """NC-1: group items into stories per ticker."""
    todo = sorted(items, key=lambda i: (i.published_at, i.item_id))
    hasher = HashingEmbedder()
    need = [i for i in todo if i.hvec is None]
    for it, v in zip(need, hasher.embed([f"{i.headline}. {i.text[:300]}" for i in need]),
                     strict=True):
        it.hvec = v
    stories: list[Story] = []
    open_by_ticker: dict[str, list[Story]] = {}
    window = timedelta(hours=window_h)
    for it in todo:
        best, best_sim = None, -1.0
        for st in open_by_ticker.get(it.ticker, []):
            if it.published_at - st.last_seen > window:
                continue
            sim = max(_joins(it, m, join, join_hash) for m in st.items)
            if sim > best_sim:
                best, best_sim = st, sim
        if best is not None and best_sim >= 0:
            best.items.append(it)
        else:
            st = Story(story_id(it), it.ticker, [it])
            stories.append(st)
            open_by_ticker.setdefault(it.ticker, []).append(st)
    return stories


def score_novelty(stories: Sequence[Story], days: int = 30) -> None:
    """NC-2: novelty = 1 - the highest similarity between a story's first item and any item
    of the same ticker's stories that started in the ``days`` before it."""
    by_ticker: dict[str, list[Story]] = {}
    for st in sorted(stories, key=lambda x: x.first_seen):
        prior = [p for p in by_ticker.get(st.ticker, [])
                 if timedelta(0) <= st.first_seen - p.first_seen <= timedelta(days=days)]
        first = st.items[0]
        best = 0.0
        for p in prior:
            for m in p.items:
                best = max(best, similarity(first, m)[0])
        st.novelty = round(max(0.0, 1.0 - best), 3)
        by_ticker.setdefault(st.ticker, []).append(st)


def source_weight(publisher: str, weights: Mapping[str, float], default: float) -> float:
    """NC-3: weight of the outlet; matched case-insensitively by substring."""
    p = (publisher or "").lower()
    for key, w in weights.items():
        if key in p:
            return w
    return default
