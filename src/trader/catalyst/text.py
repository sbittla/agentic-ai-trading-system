"""Text models for catalysts: sentence embeddings (NC-1, NC-2) and social sentiment (NC-11).

Both load on CPU in news-sentiment, like FinBERT (A3-2). Each has a dependency-free fallback,
so dev, replay and trader-core still work without PyTorch: a hashed bag of words for
similarity, and the news scorer (FinBERT or the lexicon) for posts.
"""

from __future__ import annotations

import base64
import re
import zlib
from collections.abc import Sequence
from typing import Protocol

import numpy as np
import structlog
from numpy.typing import NDArray

log = structlog.get_logger(__name__)
Vec = NDArray[np.float32]
WORD_RE = re.compile(r"[a-z0-9$][a-z0-9$.'-]*")
STOP = frozenset("a an and are as at be by for from has have in into is it its of on or that "
                 "the their this to was were will with after over says said new inc corp co "
                 "ltd stock stocks shares share".split())


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> list[Vec]: ...


class HashingEmbedder:
    """Words and word pairs hashed into 1,024 buckets, L2-normalised. Good enough to tell
    "Nvidia beats estimates" reprints apart from a new story; no model, fully deterministic."""

    name = "hash-1024"
    dim = 1024

    def embed(self, texts: Sequence[str]) -> list[Vec]:
        out = []
        for t in texts:
            words = [w for w in WORD_RE.findall((t or "").lower()) if w not in STOP]
            grams = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
            v = np.zeros(self.dim, dtype=np.float32)
            for g in grams:
                v[zlib.crc32(g.encode()) % self.dim] += 1.0
            n = float(np.linalg.norm(v))
            out.append(v / n if n > 0 else v)
        return out


class MiniLmEmbedder:
    """``sentence-transformers/all-MiniLM-L6-v2`` (Apache-2.0) through plain ``transformers``
    with mean pooling, so no extra dependency beyond FinBERT's."""

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self.tok = AutoTokenizer.from_pretrained(model)
        self.model = AutoModel.from_pretrained(model)
        self.model.eval()
        self.name = model.rsplit("/", 1)[-1][:64]

    def embed(self, texts: Sequence[str]) -> list[Vec]:
        out: list[Vec] = []
        torch = self._torch
        for i in range(0, len(texts), 32):
            batch = [t or "" for t in texts[i:i + 32]]
            enc = self.tok(batch, padding=True, truncation=True, max_length=128,
                           return_tensors="pt")
            with torch.no_grad():
                hidden = self.model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).float()
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            out += [np.asarray(r, dtype=np.float32) for r in pooled.numpy()]
        return out


def make_embedder(model: str) -> Embedder:
    try:
        return MiniLmEmbedder(model)
    except Exception as exc:   # noqa: BLE001 - torch/transformers missing or no weights
        log.warning("embedder_unavailable_using_hash", error=str(exc))
        return HashingEmbedder()


def encode_vec(v: Vec) -> str:
    return base64.b64encode(np.asarray(v, dtype=np.float16).tobytes()).decode()


def decode_vec(s: str) -> Vec:
    return np.frombuffer(base64.b64decode(s), dtype=np.float16).astype(np.float32)


def cosine(a: Vec, b: Vec) -> float:
    if a.shape != b.shape:
        return 0.0
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    return float(np.dot(a, b) / (na * nb)) if na > 0 and nb > 0 else 0.0


class Scorer(Protocol):
    def score(self, texts: Sequence[str]) -> list[float]: ...


class SocialScorer:
    """NC-11: a classifier trained on financial social posts (labels bullish / bearish /
    neutral). Score = P(bullish) - P(bearish), -1..+1."""

    def __init__(self, model: str = "StephanAkkerman/FinTwitBERT-sentiment") -> None:
        from transformers import pipeline

        self._pipe = pipeline("text-classification", model=model, top_k=None, device=-1,
                              truncation=True)
        self.name = model.rsplit("/", 1)[-1][:64]

    def score(self, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        out = []
        for res in self._pipe(list(texts), batch_size=16):
            d = {r["label"].lower(): r["score"] for r in res}
            pos = d.get("bullish", d.get("positive", 0.0))
            neg = d.get("bearish", d.get("negative", 0.0))
            out.append(float(pos - neg))
        return out


class FallbackSocial:
    """Posts scored by the news scorer when the social model is not available (logged)."""

    def __init__(self, inner: Scorer, name: str) -> None:
        self.inner = inner
        self.name = name

    def score(self, texts: Sequence[str]) -> list[float]:
        return self.inner.score(texts)


def make_social_scorer(model: str, fallback: Scorer) -> SocialScorer | FallbackSocial:
    try:
        return SocialScorer(model)
    except Exception as exc:   # noqa: BLE001
        log.warning("social_model_unavailable_using_news_scorer", error=str(exc))
        return FallbackSocial(fallback, type(fallback).__name__.lower()[:64])
