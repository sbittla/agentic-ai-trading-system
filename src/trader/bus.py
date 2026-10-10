"""In-process event bus and the cross-container outbox bus (ARCHITECTURE §1, ADR-012).

``EventBus``: every event is written to the journal *before* it is delivered (SR-4).
``InMemoryOutbox`` / ``SqlOutbox`` / ``PgOutboxBus``: cross-container messages as rows in
``journal.outbox``; Postgres adds ``LISTEN/NOTIFY`` and a role/channel guard (SEC-17).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import and_, select, update

from trader.journal import schema as s
from trader.journal.store import SqlJournal

log = structlog.get_logger(__name__)

Handler = Callable[[BaseModel], Awaitable[None] | None]


class EventBus:
    """Typed in-process bus. publish = journal write, then queue for delivery."""

    def __init__(self, journal: SqlJournal | None, now: Callable[[], datetime]) -> None:
        self.journal = journal
        self.now = now
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._queue: deque[BaseModel] = deque()
        self.published = 0

    def subscribe(self, event_type: type[BaseModel] | str, handler: Handler) -> None:
        name = event_type if isinstance(event_type, str) else event_type.__name__
        self._subs[name].append(handler)

    def publish(self, event: BaseModel) -> None:
        name = type(event).__name__
        if self.journal is not None:
            self.journal.record_event(self.now(), name, event.model_dump(mode="json"))
        self.published += 1
        self._queue.append(event)

    async def drain(self) -> int:
        """Deliver queued events until idle (handlers may publish more)."""
        delivered = 0
        while self._queue:
            ev = self._queue.popleft()
            for h in list(self._subs.get(type(ev).__name__, ())) + list(self._subs.get("*", ())):
                res = h(ev)
                if asyncio.iscoroutine(res):
                    await res
            delivered += 1
        return delivered

    def replay(self, types: dict[str, type[BaseModel]], after_id: int = 0
               ) -> list[BaseModel]:
        """Rebuild typed events from the journal (restart recovery)."""
        if self.journal is None:
            return []
        out: list[BaseModel] = []
        for row in self.journal.events(after_id, types=types.keys()):
            out.append(types[row["type"]].model_validate(row["payload"]))
        return out


# --- outbox ---------------------------------------------------------------------------------

class OutboxMessage(BaseModel):
    id: int
    channel: str
    source: str
    payload: dict[str, Any]
    created_at: datetime


CHANNELS = ("news_flags", "outbound_msg", "inbound_cmd", "heartbeat")
ROLE_CHANNELS = {"news": ("news_flags", "heartbeat"),
                 "notifier": ("inbound_cmd", "heartbeat"),
                 "console": ("inbound_cmd",),            # UI-C2: owner commands only
                 "core": CHANNELS}


class OutboxForbidden(Exception):
    """A service tried to publish on a channel its role may not use (SEC-17)."""


class InMemoryOutbox:
    """Fake OutboxBus for tests and replay; enforces the same channel guard as Postgres."""

    def __init__(self, source: str = "core") -> None:
        self.source = source
        self._rows: list[OutboxMessage] = []
        self._consumed: set[int] = set()
        self._cond = asyncio.Condition()

    def as_source(self, source: str) -> InMemoryOutbox:
        clone = InMemoryOutbox.__new__(InMemoryOutbox)
        clone.source = source
        clone._rows, clone._consumed, clone._cond = self._rows, self._consumed, self._cond
        return clone

    async def publish(self, channel: str, payload: BaseModel | dict[str, Any]) -> int:
        if channel not in ROLE_CHANNELS.get(self.source, ()):
            raise OutboxForbidden(f"role {self.source} may not publish to channel {channel}")
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        msg = OutboxMessage(id=len(self._rows) + 1, channel=channel, source=self.source,
                            payload=data, created_at=datetime.now(UTC))
        self._rows.append(msg)
        async with self._cond:
            self._cond.notify_all()
        return msg.id

    def pending(self, channel: str) -> list[OutboxMessage]:
        return [m for m in self._rows if m.channel == channel and m.id not in self._consumed]

    async def ack(self, outbox_id: int) -> None:
        self._consumed.add(outbox_id)

    async def subscribe(self, channel: str, poll_s: float = 0.5) -> AsyncIterator[OutboxMessage]:
        seen: set[int] = set()
        while True:
            for m in self.pending(channel):
                if m.id not in seen:
                    seen.add(m.id)
                    yield m
            async with self._cond:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._cond.wait(), timeout=poll_s)


class SqlOutbox:
    """Outbox on the journal database. On Postgres it LISTENs for NOTIFY wake-ups; on SQLite
    (dev) it polls. The database trigger (docker/postgres/init.sql) is the real SEC-17 guard;
    the same check runs here so a mis-wired service fails fast."""

    def __init__(self, journal: SqlJournal, source: str) -> None:
        self.journal = journal
        self.source = source

    async def publish(self, channel: str, payload: BaseModel | dict[str, Any]) -> int:
        if channel not in ROLE_CHANNELS.get(self.source, ()):
            raise OutboxForbidden(f"role {self.source} may not publish to channel {channel}")
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        oid = await asyncio.to_thread(self.journal.insert, s.outbox, {
            "created_at": datetime.now(UTC), "source": self.source, "channel": channel,
            "payload": data})
        return oid or 0

    def _pending(self, channel: str, after: int) -> list[OutboxMessage]:
        rows = self.journal.query(
            select(s.outbox).where(and_(s.outbox.c.channel == channel,
                                        s.outbox.c.consumed_at.is_(None),
                                        s.outbox.c.id > after)).order_by(s.outbox.c.id))
        return [OutboxMessage(id=r["id"], channel=r["channel"], source=r["source"],
                              payload=r["payload"], created_at=r["created_at"]) for r in rows]

    def recent(self, channel: str, since: datetime) -> list[OutboxMessage]:
        """Messages on ``channel`` since ``since``, consumed or not (state rebuild on start)."""
        rows = self.journal.query(
            select(s.outbox).where(and_(s.outbox.c.channel == channel,
                                        s.outbox.c.created_at >= since)).order_by(s.outbox.c.id))
        return [OutboxMessage(id=r["id"], channel=r["channel"], source=r["source"],
                              payload=r["payload"], created_at=r["created_at"]) for r in rows]

    async def ack(self, outbox_id: int) -> None:
        def _do() -> None:
            with self.journal.engine.begin() as c:
                c.execute(update(s.outbox).where(s.outbox.c.id == outbox_id)
                          .values(consumed_at=datetime.now(UTC)))
        await asyncio.to_thread(_do)

    # while LISTEN is up, NOTIFY wakes the loop at once and polling is only a safety net
    LISTENING_POLL_S = 15.0

    async def subscribe(self, channel: str, poll_s: float = 2.0
                        ) -> AsyncIterator[OutboxMessage]:
        last = 0
        wake = asyncio.Event()
        listening = asyncio.Event()
        listener: asyncio.Task[None] | None = None
        if not self.journal.is_sqlite:
            listener = asyncio.create_task(self._listen(channel, wake, listening))
        try:
            while True:
                for m in await asyncio.to_thread(self._pending, channel, last):
                    last = max(last, m.id)
                    yield m
                wake.clear()
                timeout = max(poll_s, self.LISTENING_POLL_S) if listening.is_set() else poll_s
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=timeout)
        finally:
            if listener:
                listener.cancel()

    async def _listen(self, channel: str, wake: asyncio.Event,
                      listening: asyncio.Event | None = None) -> None:
        """LISTEN on Postgres; any NOTIFY wakes the poll loop early. Reconnects with back-off;
        while disconnected the subscriber falls back to fast polling."""
        try:
            import psycopg
        except ImportError:
            return

        url = self.journal.url.replace("postgresql+psycopg://", "postgresql://")
        backoff = 1.0
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {channel}")
                    if listening is not None:
                        listening.set()
                    wake.set()   # catch anything inserted while we were connecting
                    backoff = 1.0
                    async for _ in conn.notifies():
                        wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # noqa: BLE001 - fall back to polling, then retry
                log.warning("outbox_listen_failed", channel=channel, error=str(exc))
            if listening is not None:
                listening.clear()
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)


PgOutboxBus = SqlOutbox
