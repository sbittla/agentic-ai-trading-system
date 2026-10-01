"""notifier service: WhatsApp out, polled replies in (ADR-013, SEC-1, SEC-19).

Holds the Twilio keys only. Sends ``outbound_msg`` rows; polls Twilio for replies (3 s while
an approval is open, 10 s in market hours, 60 s otherwise); forwards only allow-listed,
parseable commands to ``inbound_cmd``. No inbound port, no webhook, no tunnel.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from datetime import UTC, datetime

import structlog

from trader import config as config_mod
from trader.bus import SqlOutbox
from trader.calendar import MarketCalendar
from trader.journal.store import SqlJournal
from trader.notify.commands import SenderFilter, log_command, parse
from trader.notify.whatsapp import WhatsAppNotifier
from trader.services.runtime import require_env, touch_health
from trader.timeutil import to_et

log = structlog.get_logger(__name__)


class NotifierService:
    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = config_path
        self.approval_open_until = 0.0

    async def run(self) -> None:
        loaded = config_mod.load(self.config_path, check_env=True)
        cfg = loaded.config
        env = require_env("TWILIO_SID", "TWILIO_TOKEN", "WHATSAPP_FROM", "WHATSAPP_TO",
                          "WHATSAPP_ALLOWED_SENDERS")
        journal = SqlJournal(os.environ[cfg.storage.journal_url_env], create=False)
        outbox = SqlOutbox(journal, "notifier")
        wa = WhatsAppNotifier(env["TWILIO_SID"], env["TWILIO_TOKEN"], env["WHATSAPP_FROM"],
                              env["WHATSAPP_TO"])
        senders = SenderFilter.from_env(env["WHATSAPP_ALLOWED_SENDERS"], journal)
        cal = MarketCalendar()
        poll_cfg = cfg.services.notifier_poll_sec

        async def outbound() -> None:
            async for m in outbox.subscribe("outbound_msg", poll_s=1.0):
                p = m.payload
                if p.get("kind") == "approval":
                    self.approval_open_until = time.monotonic() + float(p.get("timeout_s", 90))
                await wa.send(str(p.get("text", "")), kind=str(p.get("kind", "info")))
                await outbox.ack(m.id)

        async def inbound() -> None:
            while True:
                now = datetime.now(UTC)
                try:
                    for sender, body, _sent_at in await wa.poll():
                        if not senders.accept(sender, body, now):   # SEC-1
                            continue
                        cmd = parse(body, sender, now)
                        if cmd is None:
                            log_command(journal, now, sender, body, False,
                                        "CMD_REJECTED: unparseable", "whatsapp")
                            continue
                        await outbox.publish("inbound_cmd", {
                            "name": cmd.name, "arg": cmd.arg, "sender": sender,
                            "received_at": now.isoformat(), "source": "whatsapp"})
                except Exception as exc:   # noqa: BLE001
                    log.warning("twilio_poll_failed", error=str(exc))
                local = to_et(now)
                market = cal.is_trading_day(local.date()) and 7 <= local.hour < 17
                wait = (poll_cfg.approval if time.monotonic() < self.approval_open_until
                        else poll_cfg.market_hours if market else poll_cfg.off_hours)
                await asyncio.sleep(wait)

        async def heartbeat() -> None:
            while True:
                fresh = wa.last_poll_ok is not None and time.monotonic() - wa.last_poll_ok < 120
                if fresh or wa.last_poll_ok is None:
                    touch_health("notifier")
                with contextlib.suppress(Exception):
                    await outbox.publish("heartbeat", {"service": "notifier",
                                                       "twilio_poll_ok": fresh})
                await asyncio.sleep(cfg.services.heartbeat_sec)

        await asyncio.gather(outbound(), inbound(), heartbeat())


def main(config_path: str | None = None) -> None:
    asyncio.run(NotifierService(config_path).run())
