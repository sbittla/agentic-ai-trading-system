"""Telegram commands for the alerts service.

Commands are case-insensitive, the leading ``/`` is optional and ``@BotName`` suffixes are
ignored, so ``/add@MyBot nvda > 140`` and ``add NVDA above 140`` are the same.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from trader.alerts.config import MAX_IEX_SYMBOLS, normalize_symbols

if TYPE_CHECKING:
    from trader.alerts.service import AlertService

HELP = """📟 Price alert bot
Price rules (checked on every trade):
  /add NVDA above 140      (or: NVDA > 140)
  /add NVDA below 120.5    (or: NVDA < 120.5)
  /add TSLA 250            direction from the current price
  /add AAPL up 3%          /add AAPL down 2%
  add 'repeat' to keep a rule after it fires, and any note after it:
  /add SPY below 560 repeat stop-loss zone
  /list [SYM] · /del 3 [4 5] · /clear [SYM]
Watchlist (auto signals: gap, ORB, VWAP, HOD/LOD, volume, ±moves):
  /watch AMD PLTR · /unwatch AMD · /watchlist
  /signals on|off
Info: /price [SYM ...] · /movers · /status
Quiet: /mute [minutes] · /unmute"""

ABOVE = {"above", ">", ">=", "over", "a"}
BELOW = {"below", "<", "<=", "under", "b"}
UP = {"up", "+", "gain"}
DOWN = {"down", "-", "drop", "loss"}
NUM = re.compile(r"^[+-]?\$?\d+(\.\d+)?%?$")


@dataclass(frozen=True)
class ParsedRule:
    symbol: str
    kind: str | None      # None: decide above/below from the current price
    value: float
    repeat: bool
    note: str


def _num(tok: str) -> float:
    return float(tok.replace("$", "").replace("%", "").replace(",", ""))


def parse_rule(args: list[str]) -> ParsedRule:
    """``SYM [op] VALUE [repeat] [note...]``; raises ValueError with a user-facing message."""
    if len(args) < 2:
        raise ValueError("usage: /add SYM above|below PRICE  or  /add SYM up|down PCT%")
    syms = normalize_symbols([args[0]])
    if not syms:
        raise ValueError(f"bad symbol {args[0]!r}")
    sym, rest = syms[0], args[1:]
    op = rest[0].lower()
    kind: str | None
    # glued forms: ">140", "<120", "+3%", "-2%"
    m = re.match(r"^(>=|<=|>|<)(.+)$", op)
    if m:
        op = m.group(1)
        rest = [op, m.group(2), *rest[1:]]
    elif re.match(r"^[+-]\d", op) and op.endswith("%"):
        op, rest = op[0], [op[0], op[1:], *rest[1:]]
    if op in ABOVE:
        kind, rest = "above", rest[1:]
    elif op in BELOW:
        kind, rest = "below", rest[1:]
    elif op in UP:
        kind, rest = "up_pct", rest[1:]
    elif op in DOWN:
        kind, rest = "down_pct", rest[1:]
    else:
        kind = None
    if not rest or not NUM.match(rest[0]):
        raise ValueError("expected a number, e.g. /add NVDA above 140 or /add NVDA up 3%")
    raw = rest[0]
    value = _num(raw)
    if kind is None and raw.endswith("%"):
        kind = "up_pct" if value >= 0 else "down_pct"
    value = abs(value)
    if value <= 0:
        raise ValueError("value must be positive")
    if kind in ("up_pct", "down_pct") and value > 100:
        raise ValueError("percent must be ≤ 100")
    tail = rest[1:]
    repeat = bool(tail) and tail[0].lower() in ("repeat", "r", "always")
    if repeat:
        tail = tail[1:]
    return ParsedRule(sym, kind, value, repeat, " ".join(tail))


def split_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    head = parts[0].lstrip("/").split("@", 1)[0].lower()
    return head, parts[1:]


class CommandHandler:
    def __init__(self, svc: AlertService) -> None:
        self.svc = svc

    async def handle(self, text: str) -> str:
        cmd, args = split_command(text)
        try:
            return await self._dispatch(cmd, args, text)
        except ValueError as exc:
            return f"⚠️ {exc}"

    async def _dispatch(self, cmd: str, args: list[str], text: str) -> str:
        svc = self.svc
        store = svc.store
        if cmd in ("help", "start", "h", "?"):
            return HELP
        if cmd in ("add", "alert", "set"):
            return await self._add(args)
        if cmd in ("list", "rules", "ls", "alerts"):
            syms = normalize_symbols(args[:1])
            rules = store.rules(syms[0] if syms else None)
            if not rules:
                return "No price rules. Add one: /add NVDA above 140"
            return "Price rules:\n" + "\n".join(r.describe() for r in rules)
        if cmd in ("del", "delete", "rm", "remove"):
            ids = [int(a.lstrip("#")) for a in args if a.lstrip("#").isdigit()]
            if not ids:
                raise ValueError("usage: /del ID [ID ...] (see /list)")
            gone = [i for i in ids if store.delete_rule(i)]
            await svc.rules_changed()
            missing = sorted(set(ids) - set(gone))
            msg = f"Deleted {', '.join(f'#{i}' for i in gone)}" if gone else "Nothing deleted"
            return msg + (f" (not found: {', '.join(map(str, missing))})" if missing else "")
        if cmd == "clear":
            syms = normalize_symbols(args[:1])
            n = store.clear_rules(syms[0] if syms else None)
            await svc.rules_changed()
            return f"Cleared {n} rule(s)" + (f" for {syms[0]}" if syms else "")
        if cmd in ("watch", "follow"):
            add = normalize_symbols(args)
            if not add:
                raise ValueError("usage: /watch SYM [SYM ...]")
            wl = svc.watchlist()
            new = wl + [s for s in add if s not in wl]
            if len(new) > MAX_IEX_SYMBOLS:
                raise ValueError(f"watchlist is limited to {MAX_IEX_SYMBOLS} symbols on the "
                                 "free IEX feed")
            await svc.set_watchlist(new)
            return f"Watching {len(new)}: {' '.join(new)}"
        if cmd in ("unwatch", "unfollow"):
            drop = set(normalize_symbols(args))
            wl = svc.watchlist()
            new = [s for s in wl if s not in drop]
            await svc.set_watchlist(new)
            return f"Watching {len(new)}: {' '.join(new) or '(none)'}"
        if cmd in ("watchlist", "wl"):
            wl = svc.watchlist()
            return f"Watching {len(wl)}: {' '.join(wl) or '(none)'}"
        if cmd in ("price", "p", "quote", "q"):
            return await svc.quote(normalize_symbols(args) or svc.watchlist())
        if cmd in ("movers", "top"):
            return svc.movers_text()
        if cmd == "mute":
            minutes = int(args[0]) if args and args[0].isdigit() else 60
            until = datetime.now(UTC) + timedelta(minutes=minutes)
            store.set("mute_until", until.isoformat())
            return f"🔕 Muted for {minutes} min (price rules too). /unmute to resume."
        if cmd == "unmute":
            store.set("mute_until", "")
            return "🔔 Alerts on"
        if cmd == "signals":
            if not args or args[0].lower() not in ("on", "off"):
                state = "off" if store.get("signals") == "off" else "on"
                return f"Auto signals are {state}. Use /signals on|off"
            store.set("signals", args[0].lower())
            return f"Auto signals {args[0].lower()} (price rules are unaffected)"
        if cmd in ("status", "ping"):
            return svc.status_text()
        # shorthand without a command word: "NVDA > 140", "AAPL up 3%"
        if args and normalize_symbols([cmd]):
            try:
                parse_rule([cmd, *args])
            except ValueError:
                pass
            else:
                return await self._add([cmd, *args])
        return "Unknown command. Send /help"

    async def _add(self, args: list[str]) -> str:
        svc = self.svc
        pr = parse_rule(args)
        kind = pr.kind
        last = await svc.last_price(pr.symbol)
        if kind is None:
            if last is None:
                raise ValueError(f"no price for {pr.symbol} yet: say above or below")
            kind = "above" if pr.value > last else "below"
        if len(svc.store.rules()) >= 200:
            raise ValueError("too many rules (200); /clear some first")
        if pr.symbol not in svc.subscribed() and len(svc.subscribed()) >= MAX_IEX_SYMBOLS:
            raise ValueError(f"{MAX_IEX_SYMBOLS}-symbol limit of the free IEX feed reached")
        rule = svc.store.add_rule(pr.symbol, kind, pr.value, pr.repeat, pr.note)
        await svc.rules_changed()
        now = f" (now {last:,.2f})" if last is not None else ""
        already = last is not None and (kind == "above" and last >= pr.value
                                        or kind == "below" and last <= pr.value)
        warn = "\n⚠️ already past that level: it fires on the next trade" if already else ""
        return f"✅ Added {rule.describe()}{now}{warn}"
