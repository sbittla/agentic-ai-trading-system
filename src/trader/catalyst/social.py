"""Social sentiment indicator (NC-11..NC-15).

Per ticker and day, from scored posts: mention volume and its z-score against the trailing
20 days, net sentiment, bull share and distinct authors, after noise guards. Then agreement
with the news direction and the crowding warning.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Post:
    ticker: str
    day: date
    author: str
    text_hash: str
    score: float              # -1..+1 from the social model
    tag: str | None = None    # user's own "bullish" / "bearish" label (Stocktwits)
    length: int = 100


@dataclass(frozen=True)
class SocialDay:
    ticker: str
    day: date
    mentions: int
    mention_z: float | None
    net_sentiment: float
    bull_share: float
    authors: int

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["day"] = self.day.isoformat()
        return d


def author_hash(name: str, salt: str = "catalyst") -> str:
    """Authors are stored as a salted hash: enough to count one vote each, never the name."""
    return hashlib.sha256(f"{salt}|{name}".encode()).hexdigest()[:16]


def guarded(posts: Sequence[Post], max_per_author: int = 3, min_length: int = 20
            ) -> list[Post]:
    """NC-12: drop short posts and exact duplicates; cap posts per author per day."""
    seen: set[tuple[str, date, str]] = set()
    per_author: dict[tuple[str, date, str], int] = defaultdict(int)
    out = []
    for p in sorted(posts, key=lambda x: (x.day, x.author, x.text_hash)):
        if p.length < min_length:
            continue
        key = (p.ticker, p.day, p.text_hash)
        if key in seen:
            continue
        seen.add(key)
        ak = (p.ticker, p.day, p.author)
        if per_author[ak] >= max_per_author:
            continue
        per_author[ak] += 1
        out.append(p)
    return out


def _post_view(p: Post) -> float:
    if p.tag == "bullish":
        return 1.0
    if p.tag == "bearish":
        return -1.0
    return p.score


def daily(posts: Sequence[Post], max_per_author: int = 3,
          mention_counts: Mapping[tuple[str, date], int] | None = None) -> list[SocialDay]:
    """NC-11: one row per ticker and day; the author's posts of the day count as one vote for
    sentiment. ``mention_counts`` (ApeWisdom) supplies volume where no post text exists."""
    kept = guarded(posts, max_per_author)
    by: dict[tuple[str, date], list[Post]] = defaultdict(list)
    for p in kept:
        by[(p.ticker, p.day)].append(p)
    keys = set(by) | set(mention_counts or {})
    rows: dict[str, list[SocialDay]] = defaultdict(list)
    for t, d in sorted(keys, key=lambda k: (k[0], k[1])):
        ps = by.get((t, d), [])
        votes: dict[str, list[float]] = defaultdict(list)
        for p in ps:
            votes[p.author].append(_post_view(p))
        views = [sum(v) / len(v) for v in votes.values()]
        mentions = len(ps) if ps else int((mention_counts or {}).get((t, d), 0))
        hist = [r.mentions for r in rows[t][-20:]]
        z = None
        if len(hist) >= 5:
            m = sum(hist) / len(hist)
            sd = (sum((x - m) ** 2 for x in hist) / len(hist)) ** 0.5
            z = round((mentions - m) / max(sd, 1.0), 2)
        rows[t].append(SocialDay(
            ticker=t, day=d, mentions=mentions, mention_z=z,
            net_sentiment=round(sum(views) / len(views), 3) if views else 0.0,
            bull_share=round(sum(1 for v in views if v > 0.1) / len(views), 3) if views else 0.0,
            authors=len(votes)))
    return [r for rs in rows.values() for r in rs]


def agreement(news_direction: str, s: SocialDay | None) -> str:
    """NC-13: AGREES, DISAGREES or QUIET (no unusual volume, or no data)."""
    if s is None or s.mention_z is None or s.mention_z < 1:
        return "QUIET"
    if news_direction == "mixed" or abs(s.net_sentiment) < 0.1:
        return "QUIET"
    same = (s.net_sentiment > 0) == (news_direction == "positive")
    return "AGREES" if same else "DISAGREES"


def crowded(s: SocialDay | None, ext_atr: float | None) -> str | None:
    """NC-14: very loud, one-sided chatter while price is stretched. ``ext_atr`` is the close
    minus EMA20 in ATR units. Returns 'bullish' / 'bearish' crowding or None."""
    if s is None or s.mention_z is None or ext_atr is None or s.mention_z < 3:
        return None
    if s.bull_share >= 0.8 and ext_atr >= 2:
        return "bullish"
    if s.bull_share <= 0.2 and ext_atr <= -2:
        return "bearish"
    return None
