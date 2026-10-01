"""Cross-container bus and role grants on a real Postgres (ADR-012, SEC-17).

Needs a database initialised with docker/postgres/init.sql and migrated with `trader migrate`:
    TEST_PG_CORE_URL / TEST_PG_NEWS_URL / TEST_PG_NOTIFIER_URL / TEST_PG_READONLY_URL
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import ProgrammingError

from trader.bus import SqlOutbox
from trader.journal import schema as s
from trader.journal.store import SqlJournal

URLS = {k: os.environ.get(f"TEST_PG_{k.upper()}_URL") for k in ("core", "news", "notifier",
                                                                  "readonly")}
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not all(URLS.values()), reason="needs Postgres roles")]


def j(role: str) -> SqlJournal:
    return SqlJournal(URLS[role] or "", create=False)


@pytest.mark.rule("SEC-17")
def test_role_grants() -> None:
    now = datetime.now(UTC)
    j("news").insert(s.news_items, {"item_id": "t1", "ticker": "NVDA", "source": "finnhub",
                                    "published_at": now, "received_at": now})
    j("notifier").insert(s.commands, {"ts": now, "sender": "x", "text": "KILL",
                                      "accepted": False, "reason": "test",
                                      "source": "whatsapp"})
    with pytest.raises(ProgrammingError):   # news may not write orders
        j("news").insert(s.orders, {"client_order_id": "x", "ts": now})
    with pytest.raises(ProgrammingError):   # notifier may not write decisions
        j("notifier").insert(s.decisions, {"proposal_id": "x", "ts": now})
    with pytest.raises(ProgrammingError):   # readonly is read-only
        j("readonly").insert(s.incidents, {"ts": now, "type": "x", "detail": "y"})
    assert j("readonly").rows(s.news_items)


@pytest.mark.rule("SEC-17")
async def test_outbox_round_trip_between_roles() -> None:
    core, notifier = SqlOutbox(j("core"), "core"), SqlOutbox(j("notifier"), "notifier")
    oid = await core.publish("outbound_msg", {"kind": "alert", "text": "hello"})
    got = None
    async def consume() -> None:
        nonlocal got
        async for m in notifier.subscribe("outbound_msg", poll_s=0.2):
            if m.id == oid:
                got = m
                await notifier.ack(m.id)
                return
    await asyncio.wait_for(consume(), 10)
    assert got is not None and got.source == "core" and got.payload["text"] == "hello"
    cid = await notifier.publish("inbound_cmd", {"name": "STATUS"})
    rows = j("core").rows(s.outbox, {"id": cid})
    assert rows[0]["source"] == "notifier"   # set by the trigger from the role, not the caller
