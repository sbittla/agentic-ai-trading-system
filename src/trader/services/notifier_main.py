"""notifier service: owner messages out, polled replies in (ADR-013, ADR-014, SEC-1, SEC-19).

Channel: Telegram (``TELEGRAM_BOT_TOKEN`` set, ADR-014) or Twilio WhatsApp. Holds only that
channel's keys. Sends ``outbound_msg`` rows; polls for replies (Telegram long polling; Twilio
every 3 s while an approval is open, 10 s in market hours, 60 s otherwise); forwards only
allow-listed, parseable commands to ``inbound_cmd``. No inbound port, no webhook, no tunnel.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from trader import config as config_mod
from trader.bus import SqlOutbox
from trader.calendar import MarketCalendar
from trader.journal.store import SqlJournal
from trader.notify.commands import SenderFilter, log_command, parse
from trader.services.runtime import require_env, touch_health
from trader.timeutil import to_et

log = structlog.get_logger(__name__)


def channel_from_env() -> str:
    explicit = os.environ.get("NOTIFIER_CHANNEL", "").strip().lower()
    if explicit in ("telegram", "whatsapp"):
        return explicit
    return "telegram" if os.environ.get("TELEGRAM_BOT_TOKEN") else "whatsapp"


class NotifierService:
    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = config_path
        self.approval_open_until = 0.0

    def _channel(self) -> tuple[str, Any, str]:
        """(channel, client, comma-separated allow-list of sender ids)."""
        channel = channel_from_env()
        if channel == "telegram":
            from trader.notify.telegram import TelegramNotifier

            env = require_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
            allowed = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or env["TELEGRAM_CHAT_ID"]
            client: Any = TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
            return channel, client, ",".join(f"telegram:{x.strip()}"
                                             for x in allowed.split(",") if x.strip())
        from trader.notify.whatsapp import WhatsAppNotifier

        env = require_env("TWILIO_SID", "TWILIO_TOKEN", "WHATSAPP_FROM", "WHATSAPP_TO",
                          "WHATSAPP_ALLOWED_SENDERS")
        client = WhatsAppNotifier(env["TWILIO_SID"], env["TWILIO_TOKEN"], env["WHATSAPP_FROM"],
                                  env["WHATSAPP_TO"],
                                  content_sid=os.environ.get("WHATSAPP_CONTENT_SID") or None,
                                  content_var=os.environ.get("WHATSAPP_CONTENT_VARIABLE", "1"))
        return channel, client, env["WHATSAPP_ALLOWED_SENDERS"]

    async def run(self) -> None:
        loaded = config_mod.load(self.config_path, check_env=True)
        cfg = loaded.config
        channel, client, allowed = self._channel()
        journal = SqlJournal(os.environ[cfg.storage.journal_url_env], create=False)
        outbox = SqlOutbox(journal, "notifier")
        senders = SenderFilter.from_env(allowed, journal, channel)
        cal = MarketCalendar()
        poll_cfg = cfg.services.notifier_poll_sec
        log.info("notifier_channel", channel=channel)

        approval_msgs: dict[str, tuple[int, bool, str]] = {}   # proposal -> (msg id, photo, code)

        async def outbound() -> None:
            async for m in outbox.subscribe("outbound_msg", poll_s=1.0):
                p = m.payload
                kind = str(p.get("kind", "info"))
                text = str(p.get("text", ""))
                pid = str(p.get("proposal_id", ""))
                if kind == "approval":
                    self.approval_open_until = time.monotonic() + float(p.get("timeout_s", 90))
                if channel == "telegram" and kind == "approval" and p.get("code"):
                    # AP-1/AP-2: chart + caption + YES/NO buttons carrying the one-time code
                    image = base64.b64decode(p["image_b64"]) if p.get("image_b64") else None
                    mid = await client.send_approval(text, str(p["code"]), image)
                    if mid is None:
                        await client.send(text, kind=kind)
                    else:
                        approval_msgs[pid] = (mid, image is not None, str(p["code"]))
                elif kind == "approval_update":
                    # AP-3: live status; WhatsApp has no edits, so updates are Telegram-only
                    ref = approval_msgs.get(pid)
                    if channel == "telegram" and ref is not None:
                        await client.edit_approval(ref[0], text, photo=ref[1],
                                                   code=None if p.get("final") else ref[2])
                    if p.get("final"):
                        approval_msgs.pop(pid, None)
                else:
                    await client.send(text, kind=kind)
                await outbox.ack(m.id)

        async def handle(sender: str, body: str) -> None:
            now = datetime.now(UTC)
            if not senders.accept(sender, body, now):   # SEC-1
                return
            cmd = parse(body, sender, now, source=channel)
            if cmd is None:
                log_command(journal, now, sender, body, False, "CMD_REJECTED: unparseable",
                            channel)
                return
            await outbox.publish("inbound_cmd", {
                "name": cmd.name, "arg": cmd.arg, "sender": sender,
                "received_at": now.isoformat(), "source": channel})

        async def inbound() -> None:
            prime: Callable[[], Awaitable[Any]] = (client.prime if channel == "telegram"
                                                   else client.prime_session)
            try:
                log.info("notifier_primed", result=str(await prime()))
            except Exception as exc:   # noqa: BLE001
                log.warning("notifier_prime_failed", error=str(exc))
            while True:
                now = datetime.now(UTC)
                try:
                    msgs = await (client.poll(wait_s=20) if channel == "telegram"
                                  else client.poll())
                    for sender, body, _sent_at in msgs:
                        await handle(sender, body)
                except Exception as exc:   # noqa: BLE001
                    log.warning("notifier_poll_failed", channel=channel, error=str(exc))
                    await asyncio.sleep(5)
                if channel == "telegram":
                    continue   # long polling already waited for new messages
                local = to_et(now)
                market = cal.is_trading_day(local.date()) and 7 <= local.hour < 17
                wait = (poll_cfg.approval if time.monotonic() < self.approval_open_until
                        else poll_cfg.market_hours if market else poll_cfg.off_hours)
                await asyncio.sleep(wait)

        async def heartbeat() -> None:
            while True:
                last = client.last_poll_ok
                fresh = last is not None and time.monotonic() - last < 120
                if fresh or last is None:
                    touch_health("notifier")
                with contextlib.suppress(Exception):
                    await outbox.publish("heartbeat", {"service": "notifier",
                                                       "channel": channel, "poll_ok": fresh})
                await asyncio.sleep(cfg.services.heartbeat_sec)

        await asyncio.gather(outbound(), inbound(), heartbeat())


def main(config_path: str | None = None) -> None:
    asyncio.run(NotifierService(config_path).run())
