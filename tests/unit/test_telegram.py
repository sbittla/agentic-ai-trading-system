"""Telegram notifier (ADR-014): send, long polling, allow-list, no replay of old commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from trader.notify.commands import SenderFilter, parse
from trader.notify.telegram import TelegramError, TelegramNotifier

TOKEN = "123456:TESTTOKEN"


class FakeBot:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []

    def msg(self, uid: int, chat: int, text: str) -> None:
        self.updates.append({"update_id": uid, "message": {
            "message_id": uid, "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": chat, "type": "private", "first_name": "Owner"}, "text": text}})

    def transport(self) -> httpx.MockTransport:
        def handler(req: httpx.Request) -> httpx.Response:
            method = req.url.path.rsplit("/", 1)[-1]
            body = json.loads(req.content or b"{}")
            self.calls.append((method, body))
            if method == "getMe":
                return httpx.Response(200, json={"ok": True, "result": {"username": "TestBot"}})
            if method == "sendMessage":
                if body.get("chat_id") == "bad":
                    return httpx.Response(400, json={"ok": False, "error_code": 400,
                                                     "description": "chat not found"})
                self.sent.append(body)
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            if method == "getUpdates":
                off = int(body.get("offset", 0) or 0)
                res = [u for u in self.updates if int(u["update_id"]) >= off]   # type: ignore[call-overload]
                return httpx.Response(200, json={"ok": True, "result": res})
            return httpx.Response(404, json={"ok": False, "description": "unknown"})
        return httpx.MockTransport(handler)


@pytest.mark.rule("SEC-19")
async def test_send_and_long_poll_outbound_only() -> None:
    bot = FakeBot()
    tg = TelegramNotifier(TOKEN, "42", transport=bot.transport())
    await tg.send("[PAPER] hello", kind="alert")
    assert bot.sent == [{"chat_id": "42", "text": "[PAPER] hello",
                         "disable_web_page_preview": True}]
    bot.msg(1, 42, "STATUS")
    got = await tg.poll(wait_s=0)
    assert [(s, t) for s, t, _ in got] == [("telegram:42", "STATUS")]
    assert await tg.poll(wait_s=0) == []   # offset advanced: each update once
    assert all(m != "setWebhook" for m, _ in bot.calls)


async def test_prime_skips_old_commands() -> None:
    bot = FakeBot()
    bot.msg(5, 42, "FLATTEN")   # sent before the notifier started
    tg = TelegramNotifier(TOKEN, "42", transport=bot.transport())
    assert await tg.prime() == 1
    assert await tg.poll(wait_s=0) == []
    bot.msg(6, 42, "KILL")
    assert [t for _, t, _ in await tg.poll(wait_s=0)] == ["KILL"]


@pytest.mark.rule("SEC-1")
def test_only_allow_listed_chat_ids_command(journal) -> None:  # type: ignore[no-untyped-def]
    f = SenderFilter.from_env("telegram:42", journal, "telegram")
    now = datetime.now(UTC)
    assert f.accept("telegram:42", "KILL", now)
    assert not f.accept("telegram:999", "FLATTEN", now)
    cmd = parse("YES K7Q2", "telegram:42", now, source="telegram")
    assert cmd is not None and cmd.source == "telegram"


async def test_errors_never_contain_the_token() -> None:
    bot = FakeBot()
    tg = TelegramNotifier(TOKEN, "bad", transport=bot.transport(), retries=1)
    with pytest.raises(TelegramError) as e:
        await tg._call("sendMessage", {"chat_id": "bad", "text": "x"})
    assert TOKEN not in str(e.value)
    await tg.send("x", kind="alert")   # logged, retried, never raises into trading


def test_channel_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from trader.services.notifier_main import channel_from_env

    monkeypatch.delenv("NOTIFIER_CHANNEL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert channel_from_env() == "whatsapp"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    assert channel_from_env() == "telegram"
    monkeypatch.setenv("NOTIFIER_CHANNEL", "whatsapp")
    assert channel_from_env() == "whatsapp"
