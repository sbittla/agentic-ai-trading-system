"""NF-1 fallback channel: e-mail when the Telegram/WhatsApp notifier is down.

Optional: configured with ``SMTP_HOST``, ``SMTP_PORT`` (587), ``SMTP_USER``,
``SMTP_PASSWORD`` and ``FALLBACK_EMAIL_TO`` in ``.env.core``. Without them the incident is
still journaled and shown on the owner console; a container cannot raise a Windows desktop
notification.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage

import structlog

log = structlog.get_logger(__name__)


class EmailFallback:
    def __init__(self, host: str, port: int, user: str, password: str, to: str) -> None:
        self.host, self.port, self.user, self.password, self.to = host, port, user, password, to

    @classmethod
    def from_env(cls) -> EmailFallback | None:
        e = os.environ
        if not (e.get("SMTP_HOST") and e.get("FALLBACK_EMAIL_TO")):
            return None
        return cls(e["SMTP_HOST"], int(e.get("SMTP_PORT", "587")), e.get("SMTP_USER", ""),
                   e.get("SMTP_PASSWORD", ""), e["FALLBACK_EMAIL_TO"])

    def _send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.user or self.to
        msg["To"] = self.to
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)

    async def send(self, subject: str, body: str) -> None:
        try:
            await asyncio.to_thread(self._send, subject, body)
        except Exception as exc:   # noqa: BLE001
            log.warning("fallback_email_failed", error=str(exc))
