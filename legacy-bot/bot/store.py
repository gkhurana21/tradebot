"""SQLite persistence + append-only JSONL audit log.

One Store per process. WAL mode so the paper daemon and status CLI can
read concurrently. All writes go through insert_* methods that also
append to the audit log.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from bot.models import EquitySnapshot, PaperFill, Position

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream TEXT NOT NULL,
    universe TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    price REAL NOT NULL,
    delta_units REAL NOT NULL,
    notional_usd REAL NOT NULL,
    cost_usd REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_stream_ts ON paper_fills(stream, ts DESC);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream TEXT NOT NULL,
    ts INTEGER NOT NULL,
    equity_usd REAL NOT NULL,
    cash_usd REAL NOT NULL,
    gross_exposure REAL NOT NULL,
    n_positions INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_stream_ts ON equity_snapshots(stream, ts DESC);

CREATE TABLE IF NOT EXISTS positions (
    stream TEXT NOT NULL,
    symbol TEXT NOT NULL,
    units REAL NOT NULL,
    avg_price REAL NOT NULL,
    PRIMARY KEY (stream, symbol)
);

CREATE TABLE IF NOT EXISTS stream_state (
    stream TEXT PRIMARY KEY,
    cash_usd REAL NOT NULL,
    high_water_mark REAL NOT NULL,
    halted INTEGER NOT NULL DEFAULT 0,
    last_bar_ts INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, db_path: str, audit_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._audit_path = Path(audit_path)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)

    def _audit(self, event: str, payload: dict) -> None:
        rec = {"ts": int(time.time() * 1000), "event": event, **payload}
        with self._audit_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    # ── fills ────────────────────────────────────────────────────────────
    def insert_fill(self, fill: PaperFill) -> PaperFill:
        cur = self._conn.execute(
            "INSERT INTO paper_fills (stream, universe, symbol, ts, price,"
            " delta_units, notional_usd, cost_usd, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (fill.stream, fill.universe, fill.symbol, fill.ts, fill.price,
             fill.delta_units, fill.notional_usd, fill.cost_usd, fill.reason),
        )
        self._conn.commit()
        fill.id = cur.lastrowid
        self._audit("paper_fill", asdict(fill))
        return fill

    def fills_for_stream(self, stream: str) -> list[PaperFill]:
        rows = self._conn.execute(
            "SELECT stream, universe, symbol, ts, price, delta_units,"
            " notional_usd, cost_usd, reason, id FROM paper_fills"
            " WHERE stream=? ORDER BY ts", (stream,)).fetchall()
        return [PaperFill(*r) for r in rows]

    # ── equity ───────────────────────────────────────────────────────────
    def insert_equity(self, snap: EquitySnapshot) -> EquitySnapshot:
        cur = self._conn.execute(
            "INSERT INTO equity_snapshots (stream, ts, equity_usd, cash_usd,"
            " gross_exposure, n_positions) VALUES (?,?,?,?,?,?)",
            (snap.stream, snap.ts, snap.equity_usd, snap.cash_usd,
             snap.gross_exposure, snap.n_positions),
        )
        self._conn.commit()
        snap.id = cur.lastrowid
        return snap

    def equity_curve(self, stream: str) -> list[EquitySnapshot]:
        rows = self._conn.execute(
            "SELECT stream, ts, equity_usd, cash_usd, gross_exposure,"
            " n_positions, id FROM equity_snapshots WHERE stream=?"
            " ORDER BY ts", (stream,)).fetchall()
        return [EquitySnapshot(*r) for r in rows]

    # ── positions & stream state (crash resume) ──────────────────────────
    def save_position(self, pos: Position) -> None:
        if pos.is_flat:
            self._conn.execute(
                "DELETE FROM positions WHERE stream=? AND symbol=?",
                (pos.stream, pos.symbol))
        else:
            self._conn.execute(
                "INSERT INTO positions (stream, symbol, units, avg_price)"
                " VALUES (?,?,?,?) ON CONFLICT(stream, symbol) DO UPDATE SET"
                " units=excluded.units, avg_price=excluded.avg_price",
                (pos.stream, pos.symbol, pos.units, pos.avg_price))
        self._conn.commit()

    def load_positions(self, stream: str) -> dict[str, Position]:
        rows = self._conn.execute(
            "SELECT stream, symbol, units, avg_price FROM positions"
            " WHERE stream=?", (stream,)).fetchall()
        return {r[1]: Position(*r) for r in rows}

    def save_stream_state(self, stream: str, cash_usd: float,
                          high_water_mark: float, halted: bool,
                          last_bar_ts: int) -> None:
        self._conn.execute(
            "INSERT INTO stream_state (stream, cash_usd, high_water_mark,"
            " halted, last_bar_ts) VALUES (?,?,?,?,?)"
            " ON CONFLICT(stream) DO UPDATE SET cash_usd=excluded.cash_usd,"
            " high_water_mark=excluded.high_water_mark,"
            " halted=excluded.halted, last_bar_ts=excluded.last_bar_ts",
            (stream, cash_usd, high_water_mark, int(halted), last_bar_ts))
        self._conn.commit()

    def load_stream_state(self, stream: str) -> dict | None:
        row = self._conn.execute(
            "SELECT cash_usd, high_water_mark, halted, last_bar_ts"
            " FROM stream_state WHERE stream=?", (stream,)).fetchone()
        if row is None:
            return None
        return {"cash_usd": row[0], "high_water_mark": row[1],
                "halted": bool(row[2]), "last_bar_ts": row[3]}

    def streams(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT stream FROM stream_state ORDER BY stream").fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._conn.close()
