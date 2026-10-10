"""Notifiers (ADR-013, SEC-1, SEC-7).

- ``WhatsAppNotifier`` (notifier container): sends via Twilio and **polls** Twilio's Messages
  API for replies (no inbound webhook); applies the sender allow-list.
- ``OutboxNotifier`` (trader-core): publishes messages/approvals to ``outbound_msg``; approval
  answers arrive as inbound commands and resolve the waiting future.
- ``ConsoleNotifier``: local development.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trader.models.messages import ApprovalResponse
from trader.notify.telegram import HTML_KINDS

log = structlog.get_logger(__name__)


class ConsoleNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, *, kind: str) -> None:
        self.sent.append(text)
        log.info("notify", kind=kind, text=text)

    async def request_approval(self, proposal_id: str, text: str, code: str,
                               timeout_s: int) -> ApprovalResponse:
        await self.send(text, kind="approval")
        return ApprovalResponse(proposal_id=proposal_id, answer="timeout")


class OutboxNotifier:
    """trader-core side: everything goes through the outbox to the notifier container."""

    def __init__(self, outbox: Any, now: Any) -> None:
        self.outbox = outbox
        self.now = now
        self._waiters: dict[str, asyncio.Future[str]] = {}
        self._charts: dict[str, str] = {}

    async def send(self, text: str, *, kind: str) -> None:
        await self.outbox.publish("outbound_msg", {"kind": kind, "text": text})

    def attach_chart(self, proposal_id: str, png_b64: str) -> None:
        """AP-1: the chart image goes with the next approval for this proposal."""
        self._charts[proposal_id] = png_b64

    async def update_approval(self, proposal_id: str, text: str, final: bool = False) -> None:
        """AP-3: live status edits of the approval message (Telegram)."""
        await self.outbox.publish("outbound_msg", {"kind": "approval_update", "text": text,
                                                   "proposal_id": proposal_id, "final": final})

    async def request_approval(self, proposal_id: str, text: str, code: str,
                               timeout_s: int) -> ApprovalResponse:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[proposal_id] = fut
        await self.outbox.publish("outbound_msg", {"kind": "approval", "text": text,
                                                   "proposal_id": proposal_id,
                                                   "timeout_s": timeout_s, "code": code,
                                                   "image_b64": self._charts.pop(
                                                       proposal_id, None)})
        try:
            ans = await asyncio.wait_for(fut, timeout_s)
        except TimeoutError:
            ans = "timeout"
        finally:
            self._waiters.pop(proposal_id, None)
        return ApprovalResponse(proposal_id=proposal_id, answer=ans,   # type: ignore[arg-type]
                                answered_at=self.now())

    def resolve(self, proposal_id: str, answer: str) -> None:
        fut = self._waiters.get(proposal_id)
        if fut is not None and not fut.done():
            fut.set_result(answer)


class WhatsAppNotifier:
    """Twilio WhatsApp: send with retries; poll inbound replies (outbound-only, ADR-013)."""

    def __init__(self, sid: str, token: str, from_: str, to: str, retries: int = 3,
                 content_sid: str | None = None, content_var: str = "1") -> None:
        from twilio.rest import Client

        self.client = Client(sid, token)
        self.from_ = from_
        self.to = to
        self.retries = retries
        self.content_sid = content_sid or None
        self.content_var = content_var
        self._seen: set[str] = set()
        self._since = datetime.now(UTC) - timedelta(minutes=5)
        self.last_poll_ok: float | None = None
        self.last_inbound_at: datetime | None = None

    def session_open(self, now: datetime | None = None) -> bool:
        """WhatsApp allows free-form business messages only within 24 h of the owner's last
        message; outside it a pre-approved template is required."""
        now = now or datetime.now(UTC)
        return self.last_inbound_at is not None and now - self.last_inbound_at < timedelta(
            hours=23, minutes=30)

    def message_args(self, text: str, now: datetime | None = None) -> dict[str, str]:
        if self.content_sid and not self.session_open(now):
            return {"content_sid": self.content_sid,
                    "content_variables": json.dumps({self.content_var: text[:1000]})}
        return {"body": text[:1500]}

    async def send(self, text: str, *, kind: str) -> None:
        if kind in HTML_KINDS:   # composed for Telegram HTML: WhatsApp shows plain text
            text = html.unescape(re.sub(r"</?[bi]>", "", text))
        for attempt in range(self.retries):
            try:
                await asyncio.to_thread(self.client.messages.create, from_=self.from_,
                                        to=self.to, **self.message_args(text))
                return
            except Exception as exc:   # noqa: BLE001 - twilio/requests errors
                log.warning("whatsapp_send_failed", attempt=attempt, error=str(exc))
                await asyncio.sleep(2 ** attempt)

    async def prime_session(self) -> datetime | None:
        """At start-up, find the owner's last message in the past 24 h so a WhatsApp session
        that is already open is known. These old messages are marked seen and are never
        executed as commands (an old FLATTEN must not fire again)."""
        since = datetime.now(UTC) - timedelta(hours=24)
        msgs = await asyncio.to_thread(self.client.messages.list, to=self.from_,
                                       date_sent_after=since, limit=200)
        for m in msgs:
            self._seen.add(m.sid)
            if m.direction == "inbound" and str(m.from_) == self.to and m.date_sent:
                self.last_inbound_at = max(self.last_inbound_at or m.date_sent, m.date_sent)
        self._since = datetime.now(UTC) - timedelta(seconds=5)
        return self.last_inbound_at

    async def poll(self) -> list[tuple[str, str, datetime]]:
        """Inbound messages to our number since the last poll: (sender, body, sent_at)."""
        msgs = await asyncio.to_thread(self.client.messages.list, to=self.from_,
                                       date_sent_after=self._since, limit=50)
        self.last_poll_ok = time.monotonic()
        out = []
        for m in sorted(msgs, key=lambda x: x.date_sent or datetime.now(UTC)):
            if m.sid in self._seen or m.direction != "inbound":
                continue
            self._seen.add(m.sid)
            sent = m.date_sent or datetime.now(UTC)
            out.append((str(m.from_), str(m.body or ""), sent))
            if str(m.from_) == self.to:
                self.last_inbound_at = max(self.last_inbound_at or sent, sent)
        if msgs:
            self._since = max(m.date_sent for m in msgs if m.date_sent) - timedelta(seconds=1)
        return out

    async def request_approval(self, proposal_id: str, text: str, code: str,
                               timeout_s: int) -> ApprovalResponse:
        await self.send(text, kind="approval")
        return ApprovalResponse(proposal_id=proposal_id, answer="timeout")

    async def commands(self) -> AsyncIterator[Any]:   # pragma: no cover - see notifier_main
        if False:
            yield None
