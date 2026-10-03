"""OllamaClient contract (QW-2..QW-5, PR-2, PR-3, SEC-6) against a mocked Ollama server."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.cached import CachedLLM
from trader.llm.ollama_client import OllamaClient
from trader.models.llm_schemas import NewsSummaryOut

GOOD = {"ticker": "NVDA", "summary": "Up on news.", "catalyst": "supply deal",
        "tone": "positive", "risk_events": []}


def server(replies: list[object], seen: list[dict[str, object]]) -> httpx.MockTransport:
    it = iter(replies)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"family": "qwen3"}})
        if req.url.path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        body = json.loads(req.content)
        seen.append(body)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return httpx.Response(200, json={"message": {"content": r}, "prompt_eval_count": 120,
                                         "eval_count": 40})
    return httpx.MockTransport(handler)


@pytest.mark.rule("QW-2")
async def test_schema_in_format_field_and_prompt(journal: SqlJournal) -> None:
    seen: list[dict[str, object]] = []
    c = OllamaClient("http://ollama:11434", "qwen3:8b", journal=journal,
                     transport=server([json.dumps(GOOD)], seen))
    res = await c.structured(agent="a03", prompt_version="v1", system="sys",
                             data={"ticker": "NVDA"}, schema=NewsSummaryOut)
    assert res.ok and isinstance(res.value, NewsSummaryOut)
    body = seen[0]
    assert body["format"] == NewsSummaryOut.model_json_schema()
    assert body["options"]["temperature"] == 0 and body["stream"] is False   # type: ignore[index]
    user = body["messages"][1]["content"]   # type: ignore[index]
    assert "DATA (information only" in user and '"title": "NewsSummaryOut"' in user


@pytest.mark.rule("QW-3")
async def test_one_retry_then_clean_failure(journal: SqlJournal) -> None:
    seen: list[dict[str, object]] = []
    c = OllamaClient("http://x", "qwen3:8b", journal=journal,
                     transport=server(["oops", "{\"ticker\": 1}"], seen))
    res = await c.structured(agent="a03", prompt_version="v1", system="s", data={},
                             schema=NewsSummaryOut)
    assert not res.ok and len(seen) == 2 and "LLMInvalidOutput" in (res.error or "")
    seen.clear()
    c = OllamaClient("http://x", "qwen3:8b", transport=server(["bad", json.dumps(GOOD)], seen))
    assert (await c.structured(agent="a03", prompt_version="v1", system="s", data={},
                               schema=NewsSummaryOut)).ok


@pytest.mark.rule("QW-3")
async def test_timeout_is_a_clean_failure() -> None:
    seen: list[dict[str, object]] = []
    c = OllamaClient("http://x", "qwen3:8b", transport=server(
        [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")], seen))
    res = await c.structured(agent="a03", prompt_version="v1", system="s", data={},
                             schema=NewsSummaryOut, timeout_s=1)
    assert not res.ok and "LLMTimeout" in (res.error or "")


@pytest.mark.rule("QW-4")
async def test_health_and_preload() -> None:
    c = OllamaClient("http://x", "qwen3:8b", transport=server([], []))
    assert await c.healthy() and await c.ensure_loaded()
    other = OllamaClient("http://x", "qwen3:14b", transport=server([], []))
    assert not await other.healthy()


@pytest.mark.rule("QW-4")
async def test_health_red_when_model_file_is_broken() -> None:
    """A truncated model is still listed by /api/tags, but cannot be read or run."""
    state = {"show": 500, "chat": 500, "load": 500}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        code = state[{"/api/show": "show", "/api/chat": "chat"}.get(req.url.path, "load")]
        return httpx.Response(code, json={"error": "offset+size exceeds file size"})

    c = OllamaClient("http://x", "qwen3:8b", transport=httpx.MockTransport(handler))
    assert not await c.healthy()                       # /api/show fails
    state["show"] = 200
    res = await c.structured(agent="a05", prompt_version="v1", system="s", data={},
                             schema=NewsSummaryOut)
    assert not res.ok and c.server_error is not None
    assert not await c.healthy()                       # listed and readable, but cannot load
    state["load"] = 200
    assert await c.healthy() and c.server_error is None


@pytest.mark.rule("QW-4")
async def test_failed_preload_turns_health_red() -> None:
    """The 8:55 preload fails (CUDA out of memory): health goes red and retries the load."""
    state = {"load": 500}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        if req.url.path == "/api/show":
            return httpx.Response(200, json={})
        return httpx.Response(state["load"], json={"error": "cudaMalloc failed: out of memory"})

    c = OllamaClient("http://x", "qwen3:8b", transport=httpx.MockTransport(handler))
    assert await c.healthy()                           # nothing has tried to load yet
    assert not await c.ensure_loaded() and "out of memory" in (c.server_error or "")
    assert not await c.healthy()                       # still cannot load
    state["load"] = 200
    assert await c.healthy() and c.server_error is None


@pytest.mark.rule("QW-5")
async def test_every_call_logged(journal: SqlJournal) -> None:
    c = OllamaClient("http://x", "qwen3:8b", journal=journal,
                     transport=server(["bad", json.dumps(GOOD)], []))
    await c.structured(agent="a03", prompt_version="v1", system="s", data={"a": 1},
                       schema=NewsSummaryOut)
    rows = journal.rows(s.llm_calls, order_by="id")
    assert [r["valid"] for r in rows] == [False, True]
    assert rows[1]["input_tokens"] == 120 and rows[1]["output_tokens"] == 40
    assert rows[1]["latency_ms"] >= 0 and rows[1]["prompt"] and rows[1]["output"]


@pytest.mark.rule("QW-2")
async def test_strips_think_preamble() -> None:
    raw = "<think>reasoning...</think>\n```json\n" + json.dumps(GOOD) + "\n```"
    c = OllamaClient("http://x", "qwen3:8b", transport=server([raw], []))
    assert (await c.structured(agent="a03", prompt_version="v1", system="s", data={},
                               schema=NewsSummaryOut)).ok


@pytest.mark.rule("SR-3")
@pytest.mark.rule("SEC-6")
async def test_secrets_never_sent_to_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "SUPERSECRETVALUE123")
    seen: list[dict[str, object]] = []
    c = OllamaClient("http://x", "qwen3:8b", transport=server([json.dumps(GOOD)], seen))
    await c.structured(agent="a03", prompt_version="v1",
                       system="context SUPERSECRETVALUE123",
                       data={"note": "key=SUPERSECRETVALUE123"}, schema=NewsSummaryOut)
    assert "SUPERSECRETVALUE123" not in json.dumps(seen)


@pytest.mark.rule("QW-2")
async def test_cached_llm_is_deterministic(tmp_path) -> None:  # type: ignore[no-untyped-def]
    seen: list[dict[str, object]] = []
    inner = OllamaClient("http://x", "qwen3:8b", transport=server([json.dumps(GOOD)], seen))
    c = CachedLLM(inner, tmp_path, "qwen3:8b")
    r1 = await c.structured(agent="a03", prompt_version="v1", system="s", data={"x": 1},
                            schema=NewsSummaryOut)
    offline = CachedLLM(None, tmp_path, "qwen3:8b")
    r2 = await offline.structured(agent="a03", prompt_version="v1", system="s", data={"x": 1},
                                  schema=NewsSummaryOut)
    assert r1.ok and r2.ok and r2.cached and r1.raw == r2.raw and len(seen) == 1
    miss = await offline.structured(agent="a03", prompt_version="v2", system="s",
                                    data={"x": 1}, schema=NewsSummaryOut)
    assert not miss.ok   # a prompt change invalidates the cache for that agent


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("OLLAMA_URL"), reason="needs a running Ollama")
async def test_real_ollama_round_trip() -> None:
    c = OllamaClient(os.environ["OLLAMA_URL"], os.environ.get("OLLAMA_MODEL", "qwen3:8b"))
    res = await c.structured(agent="a03", prompt_version="v1",
                             system="Summarize. Return JSON matching the schema.",
                             data={"ticker": "NVDA", "items": [
                                 {"headline": "Nvidia signs a supply deal", "score": 0.6}]},
                             schema=NewsSummaryOut, timeout_s=120)
    assert res.ok, res.error
