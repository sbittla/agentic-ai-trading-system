"""Telegram notifier for the `notifier` container (ADR-014, SEC-1, SEC-19).

Outbound-only like the WhatsApp channel (ADR-013): messages are sent with the Bot API
``sendMessage`` and replies are fetched with ``getUpdates`` long polling, so nothing on the
host is reachable from the internet and no webhook is ever registered. Only allow-listed chat
IDs can issue commands (SEC-1); senders are reported as ``telegram:<chat id>``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)
API = "https://api.telegram.org"
MAX_TEXT = 4000   # Telegram limit is 4096 characters


class TelegramError(Exception):
    pass


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, retries: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self.chat_id = str(chat_id)
        self.retries = retries
        self._transport = transport
        self._offset: int | None = None
        self.last_poll_ok: float | None = None

    def sender_id(self, chat_id: int | str) -> str:
        return f"telegram:{chat_id}"

    async def _call(self, method: str, payload: dict[str, Any], timeout_s: float = 20
                    ) -> Any:
        async with httpx.AsyncClient(base_url=API, timeout=timeout_s + 10,
                                     transport=self._transport) as c:
            r = await c.post(f"/bot{self._token}/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            # never include the token (it is part of the URL) in errors or logs (SEC-6)
            raise TelegramError(f"{method}: {data.get('error_code')} {data.get('description')}")
        return data["result"]

    async def get_me(self) -> dict[str, Any]:
        res: dict[str, Any] = await self._call("getMe", {})
        return res

    async def send(self, text: str, *, kind: str) -> None:
        for attempt in range(self.retries):
            try:
                await self._call("sendMessage", {"chat_id": self.chat_id,
                                                 "text": text[:MAX_TEXT],
                                                 "disable_web_page_preview": True})
                return
            except (httpx.HTTPError, TelegramError) as exc:
                log.warning("telegram_send_failed", attempt=attempt, error=str(exc))
                await asyncio.sleep(2 ** attempt)

    async def prime(self) -> int:
        """Skip every update that arrived before start-up: old commands (an old FLATTEN) are
        never executed. Returns how many were skipped."""
        updates = await self._call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        if updates:
            self._offset = max(u["update_id"] for u in updates) + 1
            await self._call("getUpdates", {"offset": self._offset, "timeout": 0})
        return len(updates)

    async def poll(self, wait_s: int = 20) -> list[tuple[str, str, datetime]]:
        """New messages since the last poll: (sender, text, sent_at). Long polls up to
        ``wait_s`` seconds, so replies arrive within about a second."""
        payload: dict[str, Any] = {"timeout": wait_s, "allowed_updates": ["message"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        updates = await self._call("getUpdates", payload, timeout_s=wait_s)
        self.last_poll_ok = time.monotonic()
        out = []
        for u in updates:
            self._offset = u["update_id"] + 1
            msg = u.get("message") or {}
            text = msg.get("text")
            chat = msg.get("chat") or {}
            if not text or "id" not in chat:
                continue
            out.append((self.sender_id(chat["id"]), str(text),
                        datetime.fromtimestamp(int(msg.get("date", time.time())), UTC)))
        return out

    async def recent_chats(self) -> dict[str, str]:
        """Chat IDs that have messaged the bot (setup helper; does not consume updates)."""
        updates = await self._call("getUpdates", {"timeout": 0})
        out = {}
        for u in updates:
            chat = (u.get("message") or {}).get("chat") or {}
            if "id" in chat:
                out[str(chat["id"])] = chat.get("first_name") or chat.get("title") or ""
        return out
