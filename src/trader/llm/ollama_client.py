"""OllamaClient: the only module that talks to Qwen (IF-6, QW-1..QW-5).

Schema-constrained JSON (Ollama ``format`` = the Pydantic JSON schema, repeated in the prompt),
temperature 0 with a fixed seed, 60-second timeout, one retry, then a clean failure so the
caller skips the trade (never trade without a review).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from trader.interfaces import LLMResult
from trader.journal.store import SqlJournal
from trader.llm.base import extract_json, input_hash, log_call, render_user, schema_json, scrub

log = structlog.get_logger(__name__)


class OllamaClient:
    def __init__(self, base_url: str, model: str, *, journal: SqlJournal | None = None,
                 temperature: float = 0.0, seed: int = 42, retries: int = 1,
                 num_ctx: int = 8192, keep_alive: str = "8h",
                 now: Callable[[], datetime] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.journal = journal
        self.temperature = temperature
        self.seed = seed
        self.retries = retries
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.now = now or (lambda: datetime.now(UTC))
        self._transport = transport
        self.latencies_ms: list[int] = []
        self.server_error: str | None = None   # last 5xx from a real call; cleared on success

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s,
                                 transport=self._transport)

    async def structured(self, *, agent: str, prompt_version: str, system: str,
                         data: dict[str, Any], schema: type[BaseModel],
                         timeout_s: float = 60) -> LLMResult:
        system = scrub(system)
        user = render_user(data, schema)
        ihash = input_hash(self.model, prompt_version, system, user, schema)
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "format": schema_json(schema),
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "seed": self.seed,
                        "num_ctx": self.num_ctx},
        }
        last = LLMResult(ok=False, error="not called", model=self.model)
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            raw, err, tin, tout = "", None, 0, 0
            value: BaseModel | None = None
            try:
                async with self._client(timeout_s) as c:
                    r = await asyncio.wait_for(c.post("/api/chat", json=body), timeout_s)
                r.raise_for_status()
                self.server_error = None
                js = r.json()
                raw = js.get("message", {}).get("content", "")
                tin, tout = int(js.get("prompt_eval_count", 0)), int(js.get("eval_count", 0))
                value = schema.model_validate_json(extract_json(raw))
            except (TimeoutError, httpx.TimeoutException):
                err = f"LLMTimeout after {timeout_s}s"
            except httpx.HTTPError as exc:
                err = f"LLMUnavailable: {exc}"
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
                    self.server_error = err
            except (ValidationError, ValueError) as exc:
                err = f"LLMInvalidOutput: {str(exc)[:300]}"
            ms = int((time.monotonic() - t0) * 1000)
            self.latencies_ms.append(ms)
            log_call(self.journal, agent=agent, prompt_version=prompt_version, model=self.model,
                     ihash=ihash, prompt=f"{system}\n\n{user}", output=raw,
                     valid=value is not None, error=err, latency_ms=ms, input_tokens=tin,
                     output_tokens=tout, cached=False, now=self.now())
            last = LLMResult(ok=value is not None, value=value, error=err, latency_ms=ms,
                             input_tokens=tin, output_tokens=tout, raw=raw, model=self.model)
            if value is not None:
                return last
            log.warning("llm_attempt_failed", agent=agent, attempt=attempt, error=err)
        return last

    async def healthy(self) -> bool:
        """QW-4 health check: server up, the pinned model present and readable, and calls work.

        Listing alone is not enough: a truncated model file is still listed by ``/api/tags``
        while every chat returns 500. ``/api/show`` parses the model file, and after a 5xx on
        a real call the model must load again (``ensure_loaded``) before health is green."""
        try:
            async with self._client(10) as c:
                r = await c.get("/api/tags")
                r.raise_for_status()
                names = {m.get("name") for m in r.json().get("models", [])}
                if self.model not in names and f"{self.model}:latest" not in names:
                    return False
                r = await c.post("/api/show", json={"model": self.model})
                if r.status_code != 200:
                    return False
        except httpx.HTTPError:
            return False
        if self.server_error is not None:
            if not await self.ensure_loaded():
                return False
            self.server_error = None
        return True

    async def ensure_loaded(self) -> bool:
        """QW-4: load the model by 9:00 and keep it loaded (keep_alive) through 4:30 PM.

        A failed load (e.g. CUDA out of memory) sets ``server_error`` so ``healthy`` turns red
        and retries the load on every health check, instead of staying green until a real
        call fails inside the trading window."""
        try:
            async with self._client(120) as c:
                r = await c.post("/api/generate", json={"model": self.model, "prompt": "",
                                                        "keep_alive": self.keep_alive})
            ok = r.status_code == 200
            err = f"LLMLoadFailed: HTTP {r.status_code} {r.text[:200]}"
        except httpx.HTTPError as exc:
            ok, err = False, f"LLMLoadFailed: {exc}"
        if not ok:
            self.server_error = err
        return ok

    async def gpu_status(self) -> dict[str, Any]:
        """Loaded models and how much of each is in GPU memory (Ollama ``/api/ps``)."""
        async with self._client(10) as c:
            r = await c.get("/api/ps")
            r.raise_for_status()
        out: dict[str, Any] = {}
        for m in r.json().get("models", []):
            size, vram = int(m.get("size", 0)), int(m.get("size_vram", 0))
            out[m.get("name", "?")] = {"size_gb": round(size / 1e9, 2),
                                       "vram_gb": round(vram / 1e9, 2),
                                       "gpu_pct": round(100 * vram / size) if size else 0}
        return out

    def p95_ms(self) -> int:
        xs = sorted(self.latencies_ms[-50:])
        return xs[int(len(xs) * 0.95) - 1] if xs else 0
