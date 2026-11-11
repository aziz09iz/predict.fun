"""SQLite persistence layer.

A single connection is shared by the event loop (all writes happen on the same
thread, so no cross-thread sharing is needed).  WAL mode keeps concurrent
readers happy and makes short writes cheap.
"""
from __future__ import annotations

import datetime
import sqlite3
import threading
import time
from typing import Any, Optional

from .config import DB_FILE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    coin         TEXT NOT NULL,
    market_slug  TEXT NOT NULL,
    open_ts      INTEGER NOT NULL,
    close_ts     INTEGER NOT NULL,
    open_price   REAL,
    entry_price  REAL,
    close_price  REAL,
    direction    TEXT,
    stake        REAL NOT NULL,
    odds         REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'OPEN',
    outcome      TEXT,
    pnl          REAL,
    opened_at    TEXT NOT NULL,
    settled_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_coin ON trades(coin);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_close ON trades(close_ts);

CREATE TABLE IF NOT EXISTS balance_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    type          TEXT NOT NULL,
    amount        REAL NOT NULL,
    balance_after REAL NOT NULL,
    trade_id      INTEGER,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS runtime (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path=DB_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── runtime key/value (balance, counters) ──────────────────────────────
    def _runtime_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM runtime WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def _runtime_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runtime(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_balance(self) -> float:
        return float(self._runtime_get("balance") or "0.0")

    def set_balance(self, amount: float) -> None:
        self._runtime_set("balance", f"{amount:.8f}")

    def get_initial_balance(self) -> float:
        return float(self._runtime_get("initial_balance") or "0.0")

    def set_initial_balance(self, amount: float) -> None:
        self._runtime_set("initial_balance", f"{amount:.8f}")

    def record_balance_event(
        self,
        event_type: str,
        amount: float,
        balance_after: float,
        trade_id: int | None = None,
        note: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO balance_events(ts, type, amount, balance_after, trade_id, note) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (_now_iso(), event_type, amount, balance_after, trade_id, note),
            )
            self._conn.commit()

    # ── trades ──────────────────────────────────────────────────────────────
    def insert_trade(
        self,
        coin: str,
        market_slug: str,
        open_ts: int,
        close_ts: int,
        open_price: float | None,
        entry_price: float | None,
        direction: str | None,
        stake: float,
        odds: float,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades(coin, market_slug, open_ts, close_ts, open_price, "
                "entry_price, direction, stake, odds, status, opened_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)",
                (
                    coin,
                    market_slug,
                    open_ts,
                    close_ts,
                    open_price,
                    entry_price,
                    direction,
                    stake,
                    odds,
                    _now_iso(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def settle_trade(
        self,
        trade_id: int,
        close_price: float | None,
        outcome: str,
        pnl: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET close_price = ?, outcome = ?, pnl = ?, status = 'SETTLED', "
                "settled_at = ? WHERE id = ?",
                (close_price, outcome, pnl, _now_iso(), trade_id),
            )
            self._conn.commit()

    def expire_trade(self, trade_id: int, note: str = "") -> None:
        """Mark an open trade that can no longer be settled (bot was down)."""
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET outcome = 'UNRESOLVED', pnl = 0, status = 'SETTLED', "
                "settled_at = ? WHERE id = ?",
                (_now_iso(), trade_id),
            )
            self._conn.commit()

    def mark_trade(self, trade_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE trades SET status = ? WHERE id = ?", (status, trade_id))
            self._conn.commit()

    def open_trades(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY close_ts"
            ).fetchall()

    def recent_trades(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
            if total == 0:
                return {"total": 0, "wins": 0, "losses": 0, "pushes": 0, "winrate": 0.0, "pnl": 0.0}
            row = self._conn.execute(
                "SELECT "
                " SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) wins, "
                " SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) losses, "
                " SUM(CASE WHEN outcome='PUSH' THEN 1 ELSE 0 END) pushes, "
                " SUM(COALESCE(pnl,0)) pnl "
                "FROM trades WHERE status='SETTLED'"
            ).fetchone()
        wins, losses, pushes = (row[k] or 0 for k in ("wins", "losses", "pushes"))
        settled = wins + losses + pushes
        pnl = row["pnl"] or 0.0
        winrate = (wins / settled * 100.0) if settled else 0.0
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "winrate": winrate,
            "pnl": pnl,
        }

    def get_trade(self, trade_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()

    def update_open_price(self, trade_id: int, open_price: float) -> None:
        """Refresh the authoritative open price (predict.fun startPrice) at settle."""
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET open_price = ? WHERE id = ?", (open_price, trade_id)
            )
            self._conn.commit()

    def stats_since(self, since_iso: str) -> dict[str, Any]:
        """Settlement stats for trades settled at or after ``since_iso`` (ISO-8601 UTC)."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) c FROM trades WHERE status='SETTLED' AND settled_at >= ?",
                (since_iso,),
            ).fetchone()["c"]
            if total == 0:
                return {"total": 0, "wins": 0, "losses": 0, "pushes": 0, "winrate": 0.0, "pnl": 0.0}
            row = self._conn.execute(
                "SELECT "
                " SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) wins, "
                " SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) losses, "
                " SUM(CASE WHEN outcome='PUSH' THEN 1 ELSE 0 END) pushes, "
                " SUM(COALESCE(pnl,0)) pnl "
                "FROM trades WHERE status='SETTLED' AND settled_at >= ?",
                (since_iso,),
            ).fetchone()
        wins, losses, pushes = (row[k] or 0 for k in ("wins", "losses", "pushes"))
        settled = wins + losses + pushes
        pnl = row["pnl"] or 0.0
        winrate = (wins / settled * 100.0) if settled else 0.0
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "winrate": winrate,
            "pnl": pnl,
        }

    def ledger(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM balance_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()