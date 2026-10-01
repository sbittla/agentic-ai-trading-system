"""CachedLLM: replay-deterministic LLM responses keyed by input hash (BACKTEST_ENGINE §4, RP-2).

First run calls the inner client and stores the raw response under ``llm_cache/``; later runs
read it. A prompt or model change changes the key, so only that agent's cache is invalidated.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from trader.interfaces import LLMClient, LLMResult
from trader.journal.store import SqlJournal
from trader.llm.base import extract_json, input_hash, log_call, render_user, scrub


class CachedLLM:
    def __init__(self, inner: LLMClient | None, cache_dir: str | Path, model: str,
                 journal: SqlJournal | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.inner = inner
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.model = inner.model if inner is not None else model
        self.journal = journal
        self.now = now or (lambda: datetime.now(UTC))
        self.hits = self.misses = 0

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    async def structured(self, *, agent: str, prompt_version: str, system: str,
                         data: dict[str, Any], schema: type[BaseModel],
                         timeout_s: float = 60) -> LLMResult:
        system = scrub(system)
        user = render_user(data, schema)
        key = input_hash(self.model, prompt_version, system, user, schema)
        path = self._path(key)
        if path.exists():
            self.hits += 1
            raw = json.loads(path.read_text(encoding="utf-8"))["raw"]
            try:
                value = schema.model_validate_json(extract_json(raw))
                res = LLMResult(ok=True, value=value, raw=raw, model=self.model, cached=True)
            except (ValidationError, ValueError) as exc:
                res = LLMResult(ok=False, error=f"LLMInvalidOutput: {exc}", raw=raw,
                                model=self.model, cached=True)
            log_call(self.journal, agent=agent, prompt_version=prompt_version, model=self.model,
                     ihash=key, prompt=f"{system}\n\n{user}", output=raw, valid=res.ok,
                     error=res.error, latency_ms=0, input_tokens=0, output_tokens=0,
                     cached=True, now=self.now())
            return res
        self.misses += 1
        if self.inner is None:
            return LLMResult(ok=False, error="LLMUnavailable: cache miss in offline replay",
                             model=self.model)
        res = await self.inner.structured(agent=agent, prompt_version=prompt_version,
                                          system=system, data=data, schema=schema,
                                          timeout_s=timeout_s)
        if res.ok:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"agent": agent, "prompt_version": prompt_version,
                                        "model": self.model, "raw": res.raw}),
                            encoding="utf-8")
        return res

    async def healthy(self) -> bool:
        return True if self.inner is None else await self.inner.healthy()
