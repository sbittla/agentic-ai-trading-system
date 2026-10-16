"""Owner command authentication (SECURITY.md SEC-1..SEC-3, SEC-14).

- SEC-1 ``SenderFilter`` (notifier): only allow-listed WhatsApp numbers; others ignored + logged.
- SEC-2 ``CommandAuthority`` (trader-core): approvals need the proposal's one-time 4-char code;
  codes expire with the proposal and are single use.
- SEC-3 destructive commands (FLATTEN, RESUME, UNPAUSE, RESUME LIVE) need a confirmation code
  sent back within 60 seconds.
- SEC-14 every command, accepted or rejected, is written to the journal.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import Command

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
SAFE = {"STATUS", "KILL", "ACK", "PAUSE", "HELP", "PLAN",
        "ALERTS",            # AL-4: ALERTS ON|OFF
        "FILLED", "SOLD",    # CP-2: FILLED NVDA 40 @ 132.55 (owner's own fills)
        "OPT", "CHAIN", "IV", "OPTS", "OPTIONS",   # OP-13 (read-only, no orders)
        "RECAP",             # UI-34: RECAP [sessions] - past-window recap + watchlist
        "SCAN",              # TS-19: technical scan now (read-only, no orders)
        "CATALYST"}          # NC-28: CATALYST [TICKER] news catalyst outlook (read-only)
DESTRUCTIVE = {"FLATTEN", "RESUME", "UNPAUSE"}
APPROVAL = {"YES", "NO"}
KNOWN = SAFE | DESTRUCTIVE | APPROVAL | {"CONFIRM"}


def new_code(n: int = 4) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))


def parse(text: str, sender: str, now: datetime, source: str = "whatsapp") -> Command | None:
    parts = text.strip().upper().split()
    if not parts:
        return None
    name, arg = parts[0], " ".join(parts[1:])
    if name not in KNOWN:
        if len(parts) == 1 and len(name) == 4 and all(c in CODE_ALPHABET for c in name):
            name, arg = "CONFIRM", name   # bare confirmation code (RUNBOOK §3)
        else:
            return None
    return Command(name=name, arg=arg, sender=sender, received_at=now,
                   source=command_source(source))


def command_source(value: str | None) -> Literal["whatsapp", "telegram", "local", "console"]:
    if value in ("local", "console", "telegram"):
        return value   # type: ignore[return-value]
    return "whatsapp"


def log_command(journal: SqlJournal | None, ts: datetime, sender: str, text: str,
                accepted: bool, reason: str, source: str) -> None:
    if journal is not None:
        journal.insert(s.commands, {"ts": ts, "sender": sender, "text": text,
                                    "accepted": accepted, "reason": reason, "source": source})


@dataclass
class SenderFilter:
    """SEC-1: notifier-side allow-list."""

    allowed: frozenset[str]
    journal: SqlJournal | None = None
    channel: str = "whatsapp"

    @classmethod
    def from_env(cls, value: str, journal: SqlJournal | None = None,
                 channel: str = "whatsapp") -> SenderFilter:
        return cls(frozenset(x.strip() for x in value.split(",") if x.strip()), journal,
                   channel)

    def accept(self, sender: str, text: str, now: datetime) -> bool:
        ok = sender in self.allowed
        if not ok:
            log_command(self.journal, now, sender, text, False, "CMD_REJECTED: sender not on "
                        "allow-list (SEC-1)", self.channel)
        return ok


@dataclass
class _Pending:
    code: str
    expires: datetime
    payload: str


@dataclass
class CommandAuthority:
    """trader-core side of SEC-2 / SEC-3."""

    now: Callable[[], datetime]
    journal: SqlJournal | None = None
    confirm_window_s: int = 60
    approvals: dict[str, _Pending] = field(default_factory=dict)       # code -> proposal
    confirmations: dict[str, _Pending] = field(default_factory=dict)   # code -> command
    used: set[str] = field(default_factory=set)

    def issue_approval_code(self, proposal_id: str, expires: datetime) -> str:
        code = new_code()
        while code in self.approvals or code in self.used:
            code = new_code()
        self.approvals[code] = _Pending(code, expires, proposal_id)
        return code

    def handle(self, cmd: Command) -> tuple[str, str, str]:
        """Returns (action, argument, reply). action is one of: APPROVE, REJECT_TRADE, KILL,
        STATUS, ACK, PAUSE, FLATTEN, RESUME, UNPAUSE, NEED_CONFIRM, REJECTED."""
        now = self.now()
        text = f"{cmd.name} {cmd.arg}".strip()
        if cmd.name in APPROVAL:
            code = cmd.arg.strip()
            p = self.approvals.get(code)
            if p is None or code in self.used:
                return self._rej(cmd, text, "unknown or used approval code (SEC-2)")
            if now > p.expires:
                del self.approvals[code]
                return self._rej(cmd, text, "approval code expired (SEC-2)")
            self.used.add(code)
            del self.approvals[code]
            self._ok(cmd, text, f"{cmd.name} {p.payload}")
            return ("APPROVE" if cmd.name == "YES" else "REJECT_TRADE"), p.payload, \
                f"{cmd.name} recorded"
        if cmd.name in SAFE:
            self._ok(cmd, text, cmd.name)
            return cmd.name, cmd.arg, f"{cmd.name} ok"
        if cmd.name in DESTRUCTIVE:
            if cmd.source == "local":
                self._ok(cmd, text, "local command")
                return cmd.name, cmd.arg, f"{cmd.name} ok"
            code = new_code()
            self.confirmations[code] = _Pending(code, now + timedelta(
                seconds=self.confirm_window_s), text)
            self._ok(cmd, text, f"confirmation code issued {code}")
            return "NEED_CONFIRM", code, f"Send {code} within {self.confirm_window_s}s to " \
                f"confirm {text}"
        if cmd.name == "CONFIRM":
            code = cmd.arg.strip()
            p = self.confirmations.pop(code, None)
            if p is None:
                return self._rej(cmd, text, "unknown confirmation code (SEC-3)")
            if now > p.expires:
                return self._rej(cmd, text, "confirmation code expired (SEC-3)")
            name, _, arg = p.payload.partition(" ")
            self._ok(cmd, text, f"confirmed {p.payload}")
            return name, arg, f"{p.payload} confirmed"
        return self._rej(cmd, text, "unknown command")

    def _ok(self, cmd: Command, text: str, reason: str) -> None:
        log_command(self.journal, self.now(), cmd.sender, text, True, f"CMD_ACCEPTED: {reason}",
                    cmd.source)

    def _rej(self, cmd: Command, text: str, reason: str) -> tuple[str, str, str]:
        log_command(self.journal, self.now(), cmd.sender, text, False, f"CMD_REJECTED: {reason}",
                    cmd.source)
        return "REJECTED", "", reason
