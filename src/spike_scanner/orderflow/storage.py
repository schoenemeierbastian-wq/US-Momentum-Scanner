from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from spike_scanner.orderflow.models import OrderflowMeasurement, ScannerContext


class OrderflowStorage:
    """Eigene Tabellen im vorhandenen SQLite-File.

    Bestehende Scanner-Tabellen werden nur gelesen, niemals verändert.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.database_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @staticmethod
    def _ensure_column(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _initialise(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS orderflow_measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    scan_run_id TEXT,
                    scan_rank INTEGER,
                    scanner_score REAL,
                    reference_price REAL,
                    depth_levels INTEGER NOT NULL,
                    data_quality TEXT NOT NULL,
                    pressure TEXT NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    midpoint REAL,
                    spread_abs REAL,
                    spread_pct REAL,
                    spread_bps REAL,
                    weighted_bid_depth REAL,
                    weighted_ask_depth REAL,
                    book_imbalance REAL,
                    top_imbalance REAL,
                    microprice REAL,
                    microprice_edge_bps REAL,
                    liquidity_status TEXT,
                    liquidity_reason TEXT,
                    trade_delta_ratio REAL,
                    aggressive_buy_volume REAL,
                    aggressive_sell_volume REAL,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    raw_book_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_orderflow_symbol_time
                ON orderflow_measurements(symbol, timestamp DESC);

                CREATE INDEX IF NOT EXISTS idx_orderflow_scan_run
                ON orderflow_measurements(scan_run_id, scan_rank);
                """
            )
            # Sichere Migration vorhandener Phase-1-Datenbanken.
            self._ensure_column(con, "orderflow_measurements", "liquidity_status", "TEXT")
            self._ensure_column(con, "orderflow_measurements", "liquidity_reason", "TEXT")

    def save(self, measurement: OrderflowMeasurement) -> int:
        row = measurement.to_dict()
        with self.connect() as con:
            cursor = con.execute(
                """
                INSERT INTO orderflow_measurements (
                    timestamp, symbol, provider, scan_run_id, scan_rank,
                    scanner_score, reference_price, depth_levels, data_quality,
                    pressure, best_bid, best_ask, midpoint, spread_abs,
                    spread_pct, spread_bps, weighted_bid_depth,
                    weighted_ask_depth, book_imbalance, top_imbalance,
                    microprice, microprice_edge_bps, liquidity_status,
                    liquidity_reason, trade_delta_ratio, aggressive_buy_volume,
                    aggressive_sell_volume, warnings_json, raw_book_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    row["timestamp"], row["symbol"], row["provider"],
                    row["scan_run_id"], row["scan_rank"], row["scanner_score"],
                    row["reference_price"], row["depth_levels"], row["data_quality"],
                    row["pressure"], row["best_bid"], row["best_ask"], row["midpoint"],
                    row["spread_abs"], row["spread_pct"], row["spread_bps"],
                    row["weighted_bid_depth"], row["weighted_ask_depth"],
                    row["book_imbalance"], row["top_imbalance"], row["microprice"],
                    row["microprice_edge_bps"], row["liquidity_status"],
                    row["liquidity_reason"], row["trade_delta_ratio"],
                    row["aggressive_buy_volume"], row["aggressive_sell_volume"],
                    json.dumps(row["warnings"], ensure_ascii=False),
                    json.dumps(row["raw_book"], ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def latest_top_contexts(self, limit: int = 10) -> list[ScannerContext]:
        """Liest nur Top-Kandidaten des zuletzt gespeicherten Scanner-Laufs."""
        try:
            with self.connect() as con:
                rows = con.execute(
                    """
                    SELECT o.symbol, o.run_id, o.rank, o.signal_score, o.price
                    FROM observations o
                    WHERE o.selected_top = 1
                      AND o.run_id = (
                          SELECT run_id FROM scan_runs ORDER BY timestamp DESC LIMIT 1
                      )
                    ORDER BY o.rank ASC
                    LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            ScannerContext(
                symbol=str(row["symbol"]),
                run_id=str(row["run_id"]) if row["run_id"] is not None else None,
                rank=int(row["rank"]) if row["rank"] is not None else None,
                scanner_score=(
                    float(row["signal_score"]) if row["signal_score"] is not None else None
                ),
                reference_price=float(row["price"]) if row["price"] is not None else None,
            )
            for row in rows
        ]

    def latest_measurements(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT * FROM orderflow_measurements
                ORDER BY timestamp DESC, id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
            item["raw_book"] = json.loads(item.pop("raw_book_json") or "{}")
            result.append(item)
        return result
