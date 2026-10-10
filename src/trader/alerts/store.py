"""SQLite state for the alerts service: price rules, watchlist, settings, alert log.

Standard-library ``sqlite3`` only; every call is a few microseconds, so it runs inline on
the event loop. Survives restarts (mount the file on a volume in Docker).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,          -- above | below | up_pct | down_pct
    value REAL NOT NULL,
    repeat INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    price REAL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_log_ts ON alert_log (ts);
"""
RULE_KINDS = ("above", "below", "up_pct", "down_pct")


@dataclass(frozen=True)
class Rule:
    id: int
    symbol: str
    kind: str
    value: float
    repeat: bool = False
    note: str = ""

    def describe(self) -> str:
        what = {"above": f"≥ {self.value:g}", "below": f"≤ {self.value:g}",
                "up_pct": f"up {self.value:g}% on the day",
                "down_pct": f"down {self.value:g}% on the day"}[self.kind]
        extra = " (repeat)" if self.repeat else ""
        note = f" — {self.note}" if self.note else ""
        return f"#{self.id} {self.symbol} {what}{extra}{note}"


class AlertStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)   # autocommit
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # --- rules -------------------------------------------------------------------------
    def add_rule(self, symbol: str, kind: str, value: float, repeat: bool = False,
                 note: str = "") -> Rule:
        if kind not in RULE_KINDS:
            raise ValueError(f"unknown rule kind {kind}")
        cur = self.db.execute(
            "INSERT INTO rules (symbol, kind, value, repeat, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, kind, value, int(repeat), note[:80], datetime.now(UTC).isoformat()))
        return Rule(int(cur.lastrowid or 0), symbol, kind, value, repeat, note[:80])

    def rules(self, symbol: str | None = None) -> list[Rule]:
        sql = "SELECT id, symbol, kind, value, repeat, note FROM rules"
        args: tuple[str, ...] = ()
        if symbol:
            sql += " WHERE symbol = ?"
            args = (symbol,)
        return [Rule(r[0], r[1], r[2], r[3], bool(r[4]), r[5])
                for r in self.db.execute(sql + " ORDER BY symbol, id", args)]

    def delete_rule(self, rule_id: int) -> bool:
        return self.db.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount > 0

    def clear_rules(self, symbol: str | None = None) -> int:
        if symbol:
            return self.db.execute("DELETE FROM rules WHERE symbol = ?", (symbol,)).rowcount
        return self.db.execute("DELETE FROM rules").rowcount

    # --- watchlist ---------------------------------------------------------------------
    def watchlist(self) -> list[str] | None:
        """None until the owner edits it (then the config watchlist is the default)."""
        if self.get("watchlist_set") != "1":
            return None
        return [r[0] for r in self.db.execute("SELECT symbol FROM watchlist ORDER BY position")]

    def set_watchlist(self, symbols: list[str]) -> None:
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM watchlist")
        self.db.executemany("INSERT INTO watchlist (symbol, position) VALUES (?, ?)",
                            [(s, i) for i, s in enumerate(symbols)])
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('watchlist_set', "
                        "'1')")
        self.db.execute("COMMIT")

    # --- settings ----------------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (key, value))

    # --- alert log ---------------------------------------------------------------------
    def log_alert(self, ts: datetime, symbol: str, kind: str, price: float | None,
                  text: str) -> None:
        self.db.execute("INSERT INTO alert_log (ts, symbol, kind, price, text) "
                        "VALUES (?, ?, ?, ?, ?)", (ts.isoformat(), symbol, kind, price, text))

    def alerts_since(self, since: datetime) -> list[tuple[str, str, str]]:
        return [(r[0], r[1], r[2]) for r in self.db.execute(
            "SELECT ts, symbol, kind FROM alert_log WHERE ts >= ? ORDER BY id",
            (since.isoformat(),))]

    def prune_log(self, before: datetime) -> None:
        self.db.execute("DELETE FROM alert_log WHERE ts < ?", (before.isoformat(),))

    def close(self) -> None:
        self.db.close()
