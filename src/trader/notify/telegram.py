"""Telegram notifier for the `notifier` container (ADR-014, SEC-1, SEC-19).

Outbound-only like the WhatsApp channel (ADR-013): messages are sent with the Bot API
``sendMessage`` and replies are fetched with ``getUpdates`` long polling, so nothing on the
host is reachable from the internet and no webhook is ever registered. Only allow-listed chat
IDs can issue commands (SEC-1); senders are reported as ``telegram:<chat id>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)
API = "https://api.telegram.org"
MAX_TEXT = 4000   # Telegram limit is 4096 characters
HTML_KINDS = {"next_day_ideas"}   # composed with escaped HTML (parse_mode=HTML)


class TelegramError(Exception):
    pass


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line breaks where possible so long messages are never cut off."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text or not chunks:
        chunks.append(text)
    return chunks


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, retries: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self.chat_id = str(chat_id)
        self.retries = retries
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._offset: int | None = None
        self.last_poll_ok: float | None = None

    def sender_id(self, chat_id: int | str) -> str:
        return f"telegram:{chat_id}"

    def _http(self) -> httpx.AsyncClient:
        # one pooled client for the life of the process: no TLS handshake per message/poll
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=API, transport=self._transport,
                                             timeout=httpx.Timeout(30, connect=10))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any], timeout_s: float = 20
                    ) -> Any:
        r = await self._http().post(f"/bot{self._token}/{method}", json=payload,
                                    timeout=timeout_s + 10)
        data = r.json()
        if not data.get("ok"):
            retry = (data.get("parameters") or {}).get("retry_after")
            # never include the token (it is part of the URL) in errors or logs (SEC-6)
            raise TelegramError(f"{method}: {data.get('error_code')} {data.get('description')}"
                                + (f" retry_after={retry}" if retry else ""))
        return data["result"]

    async def get_me(self) -> dict[str, Any]:
        res: dict[str, Any] = await self._call("getMe", {})
        return res

    async def send(self, text: str, *, kind: str, chat_id: str | None = None) -> bool:
        """Send ``text`` (split at 4000 characters) to ``chat_id`` or the owner chat.
        Returns False if every retry failed."""
        target = str(chat_id) if chat_id is not None else self.chat_id
        chunks = split_text(text)
        # HTML only when the message goes out whole: a tag cut between chunks is refused
        html = kind in HTML_KINDS and len(chunks) == 1
        for chunk in chunks:
            if not await self._send_one(target, chunk, html=html):
                return False
        return True

    async def _send_one(self, chat_id: str, text: str, *, html: bool = False) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": True}
        if html:
            payload["parse_mode"] = "HTML"
        for attempt in range(self.retries):
            try:
                await self._call("sendMessage", payload)
                return True
            except (httpx.HTTPError, TelegramError) as exc:
                log.warning("telegram_send_failed", attempt=attempt, error=str(exc))
                wait = 2 ** attempt
                if "retry_after=" in str(exc):   # 429 flood control: wait as told
                    with contextlib.suppress(ValueError):
                        wait = max(wait, int(str(exc).rsplit("retry_after=", 1)[1]))
                await asyncio.sleep(min(wait, 60))
        return False

    # -- approvals with a chart, buttons and live edits (AP-1..AP-3) ------------------------
    @staticmethod
    def approval_buttons(code: str) -> dict[str, Any]:
        """AP-2: the one-time code rides in the button; only allow-listed chats see it."""
        return {"inline_keyboard": [[{"text": f"✅ YES {code}", "callback_data": f"YES {code}"},
                                     {"text": f"❌ NO {code}", "callback_data": f"NO {code}"}]]}

    async def send_approval(self, text: str, code: str, image: bytes | None = None,
                            chat_id: str | None = None) -> int | None:
        """Photo + caption + buttons when a chart is attached (AP-1), else text + buttons.
        Returns the message id for live edits (AP-3), or None if sending failed."""
        target = str(chat_id) if chat_id is not None else self.chat_id
        markup = self.approval_buttons(code)
        for attempt in range(self.retries):
            try:
                if image:
                    r = await self._http().post(
                        f"/bot{self._token}/sendPhoto",
                        data={"chat_id": target, "caption": text[:1000],
                              "reply_markup": json.dumps(markup)},
                        files={"photo": ("chart.png", image, "image/png")}, timeout=40)
                    data = r.json()
                    if not data.get("ok"):
                        raise TelegramError(f"sendPhoto: {data.get('error_code')} "
                                            f"{data.get('description')}")
                    res = data["result"]
                else:
                    res = await self._call("sendMessage", {
                        "chat_id": target, "text": text[:MAX_TEXT], "reply_markup": markup,
                        "disable_web_page_preview": True})
                return int(res["message_id"])
            except (httpx.HTTPError, TelegramError) as exc:
                log.warning("telegram_approval_failed", attempt=attempt, error=str(exc))
                await asyncio.sleep(min(2 ** attempt, 10))
        return None

    async def edit_approval(self, message_id: int, text: str, *, photo: bool,
                            code: str | None = None, chat_id: str | None = None) -> bool:
        """AP-3: refresh the caption/text (live price, distance, seconds left)."""
        target = str(chat_id) if chat_id is not None else self.chat_id
        payload: dict[str, Any] = {"chat_id": target, "message_id": message_id}
        if code:
            payload["reply_markup"] = self.approval_buttons(code)
        try:
            if photo:
                await self._call("editMessageCaption", {**payload, "caption": text[:1000]})
            else:
                await self._call("editMessageText", {**payload, "text": text[:MAX_TEXT]})
            return True
        except (httpx.HTTPError, TelegramError) as exc:
            if "not modified" not in str(exc):
                log.warning("telegram_edit_failed", error=str(exc))
            return False

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
        payload: dict[str, Any] = {"timeout": wait_s,
                                   "allowed_updates": ["message", "callback_query"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        updates = await self._call("getUpdates", payload, timeout_s=wait_s)
        self.last_poll_ok = time.monotonic()
        out = []
        for u in updates:
            self._offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if cb:   # AP-2: a button press arrives as the same text a typed reply would be
                chat = ((cb.get("message") or {}).get("chat") or {})
                data = cb.get("data")
                with contextlib.suppress(httpx.HTTPError, TelegramError):
                    await self._call("answerCallbackQuery", {"callback_query_id": cb["id"],
                                                             "text": f"{data} sent"})
                if data and "id" in chat:
                    out.append((self.sender_id(chat["id"]), str(data), datetime.now(UTC)))
                continue
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