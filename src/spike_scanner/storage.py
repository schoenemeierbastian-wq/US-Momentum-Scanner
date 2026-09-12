from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from spike_scanner.models import ScanResult


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Storage:
    """SQLite persistence for scans, prospective learning events and models."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    run_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    universe_count INTEGER NOT NULL,
                    error_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    signal_score REAL NOT NULL,
                    risk_score REAL NOT NULL,
                    model_probability REAL,
                    model_probabilities_json TEXT NOT NULL DEFAULT '{}',
                    rank INTEGER NOT NULL,
                    selected_top INTEGER NOT NULL,
                    features_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    errors_json TEXT NOT NULL,
                    UNIQUE(run_id, symbol),
                    FOREIGN KEY(run_id) REFERENCES scan_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_observations_symbol_time
                ON observations(symbol, timestamp);

                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    first_seen_price REAL NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_seen_price REAL NOT NULL,
                    first_run_id TEXT NOT NULL,
                    last_run_id TEXT NOT NULL,
                    first_rank INTEGER NOT NULL,
                    last_rank INTEGER NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    ended_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_recommendations_symbol_time
                ON recommendations(symbol, first_seen_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recommendations_active_symbol
                ON recommendations(symbol) WHERE active = 1;

                CREATE TABLE IF NOT EXISTS market_bars (
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_market_bars_symbol_time
                ON market_bars(symbol, timestamp);

                CREATE TABLE IF NOT EXISTS learning_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    source_run_id TEXT NOT NULL,
                    signal_timestamp TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    horizon_hours REAL NOT NULL DEFAULT 24,
                    symbol TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    signal_score REAL NOT NULL,
                    risk_score REAL NOT NULL,
                    source_rank INTEGER NOT NULL,
                    features_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    finalized_at TEXT,
                    max_high REAL,
                    min_low REAL,
                    final_price REAL,
                    max_return REAL,
                    max_drawdown REAL,
                    bars_found INTEGER NOT NULL DEFAULT 0,
                    data_quality TEXT NOT NULL DEFAULT 'pending',
                    labels_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT,
                    FOREIGN KEY(source_run_id) REFERENCES scan_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_learning_events_due
                ON learning_events(status, due_at);
                CREATE INDEX IF NOT EXISTS idx_learning_events_symbol_time
                ON learning_events(symbol, signal_timestamp);

                CREATE TABLE IF NOT EXISTS model_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trained_at TEXT NOT NULL,
                    horizon_hours REAL NOT NULL,
                    threshold_return REAL NOT NULL,
                    rows INTEGER NOT NULL,
                    positives INTEGER NOT NULL,
                    test_rows INTEGER NOT NULL,
                    average_precision REAL,
                    brier_score REAL,
                    precision_at_0_5 REAL,
                    recall_at_0_5 REAL,
                    incumbent_average_precision REAL,
                    incumbent_brier_score REAL,
                    accepted INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    model_path TEXT NOT NULL,
                    metrics_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_model_runs_target_time
                ON model_runs(horizon_hours, threshold_return, trained_at);
                """
            )
            self._ensure_column(
                con,
                "observations",
                "model_probabilities_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                con, "learning_events", "horizon_hours", "REAL NOT NULL DEFAULT 24"
            )
            self._ensure_column(
                con,
                "learning_events",
                "data_quality",
                "TEXT NOT NULL DEFAULT 'pending'",
            )
            # Preserve the true horizon of events created by older versions.
            con.execute(
                """
                UPDATE learning_events
                SET horizon_hours = ROUND(
                    (julianday(due_at) - julianday(signal_timestamp)) * 24.0, 4
                )
                WHERE signal_timestamp IS NOT NULL AND due_at IS NOT NULL
                  AND ABS(
                    horizon_hours -
                    ((julianday(due_at) - julianday(signal_timestamp)) * 24.0)
                  ) > 0.01
                """
            )
            # Beim ersten Start dieser Version die Top-Kandidaten des zuletzt
            # gespeicherten Scans als aktuelle Empfehlung übernehmen. Frühere
            # Historie wird bewusst nicht rückwirkend geraten.
            existing = con.execute(
                "SELECT COUNT(*) FROM recommendations"
            ).fetchone()[0]
            if int(existing) == 0:
                con.execute(
                    """
                    INSERT OR IGNORE INTO recommendations
                    (symbol, first_seen_at, first_seen_price, last_seen_at,
                     last_seen_price, first_run_id, last_run_id, first_rank,
                     last_rank, seen_count, active, ended_at)
                    SELECT o.symbol, o.timestamp, o.price, o.timestamp, o.price,
                           o.run_id, o.run_id, o.rank, o.rank, 1, 1, NULL
                    FROM observations o
                    WHERE o.selected_top = 1
                      AND o.run_id = (
                          SELECT run_id FROM scan_runs ORDER BY timestamp DESC LIMIT 1
                      )
                    """
                )

    @staticmethod
    def _ensure_column(
        con: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def save_scan(self, result: ScanResult) -> None:
        top_symbols = {candidate.symbol for candidate in result.candidates}
        with self.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO scan_runs
                (run_id, timestamp, mode, universe_count, error_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.timestamp,
                    result.mode,
                    result.universe_count,
                    json.dumps(result.errors, ensure_ascii=False),
                ),
            )
            for candidate in result.all_observations:
                con.execute(
                    """
                    INSERT OR REPLACE INTO observations
                    (run_id, timestamp, symbol, price, signal_score, risk_score,
                     model_probability, model_probabilities_json, rank, selected_top,
                     features_json, reasons_json, errors_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        candidate.timestamp,
                        candidate.symbol,
                        candidate.price,
                        candidate.signal_score,
                        candidate.risk_score,
                        candidate.model_probability,
                        json.dumps(candidate.model_probabilities, ensure_ascii=False),
                        candidate.rank,
                        1 if candidate.symbol in top_symbols else 0,
                        json.dumps(candidate.features, ensure_ascii=False),
                        json.dumps(candidate.reasons, ensure_ascii=False),
                        json.dumps(candidate.errors, ensure_ascii=False),
                    ),
                )

            # Empfehlungsepisoden separat speichern. first_seen_at und
            # first_seen_price bleiben innerhalb einer laufenden Empfehlung
            # unverändert; nur last_seen_* wird bei Folgescans aktualisiert.
            if result.all_observations or not result.errors:
                current_top = {candidate.symbol.upper(): candidate for candidate in result.candidates}
                active_rows = con.execute(
                    "SELECT id, symbol FROM recommendations WHERE active = 1"
                ).fetchall()
                for row in active_rows:
                    symbol = str(row["symbol"]).upper()
                    if symbol not in current_top:
                        con.execute(
                            "UPDATE recommendations SET active=0, ended_at=? WHERE id=?",
                            (result.timestamp, int(row["id"])),
                        )

                for symbol, candidate in current_top.items():
                    active = con.execute(
                        "SELECT * FROM recommendations WHERE symbol=? AND active=1 LIMIT 1",
                        (symbol,),
                    ).fetchone()
                    if active:
                        con.execute(
                            """
                            UPDATE recommendations
                            SET last_seen_at=?, last_seen_price=?, last_run_id=?,
                                last_rank=?, seen_count=seen_count+1
                            WHERE id=?
                            """,
                            (
                                result.timestamp, float(candidate.price), result.run_id,
                                int(candidate.rank), int(active["id"]),
                            ),
                        )
                    else:
                        con.execute(
                            """
                            INSERT INTO recommendations
                            (symbol, first_seen_at, first_seen_price, last_seen_at,
                             last_seen_price, first_run_id, last_run_id, first_rank,
                             last_rank, seen_count, active, ended_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, NULL)
                            """,
                            (
                                symbol, result.timestamp, float(candidate.price),
                                result.timestamp, float(candidate.price), result.run_id,
                                result.run_id, int(candidate.rank), int(candidate.rank),
                            ),
                        )

    def recommendations_for(
        self, symbols: list[str] | tuple[str, ...] | set[str]
    ) -> dict[str, dict[str, Any]]:
        """Return the newest recommendation episode for each requested symbol."""
        cleaned = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if not cleaned:
            return {}
        placeholders = ",".join("?" for _ in cleaned)
        with self.connect() as con:
            rows = con.execute(
                f"""
                SELECT r.*
                FROM recommendations r
                JOIN (
                    SELECT symbol, MAX(id) AS max_id
                    FROM recommendations
                    WHERE symbol IN ({placeholders})
                    GROUP BY symbol
                ) latest ON latest.max_id = r.id
                """,
                tuple(cleaned),
            ).fetchall()
        return {str(row["symbol"]).upper(): dict(row) for row in rows}

    def recommendation_history(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT * FROM recommendations
                WHERE symbol=? ORDER BY first_seen_at DESC, id DESC LIMIT ?
                """,
                (str(symbol).upper(), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_market_bars(self, symbol: str, bars: pd.DataFrame) -> int:
        if bars is None or bars.empty:
            return 0
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if not required.issubset(bars.columns):
            return 0
        frame = bars[list(required)].copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna().drop_duplicates(subset=["timestamp"])
        if frame.empty:
            return 0
        captured_at = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                symbol.upper(),
                row.timestamp.isoformat(),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume),
                captured_at,
            )
            for row in frame.itertuples(index=False)
        ]
        with self.connect() as con:
            before = con.total_changes
            con.executemany(
                """
                INSERT OR REPLACE INTO market_bars
                (symbol, timestamp, open, high, low, close, volume, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return con.total_changes - before

    def market_bars_frame(
        self, symbol: str, start: str | datetime, end: str | datetime
    ) -> pd.DataFrame:
        with self.connect() as con:
            frame = pd.read_sql_query(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM market_bars
                WHERE symbol = ? AND timestamp > ? AND timestamp <= ?
                ORDER BY timestamp
                """,
                con,
                params=(
                    symbol.upper(),
                    _utc(start).isoformat(),
                    _utc(end).isoformat(),
                ),
            )
        if not frame.empty:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, format="mixed")
        return frame

    def latest_market_bar_time(self, symbol: str) -> str | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT MAX(timestamp) AS timestamp FROM market_bars WHERE symbol = ?",
                (symbol.upper(),),
            ).fetchone()
        return str(row["timestamp"]) if row and row["timestamp"] else None

    def observations_frame(self) -> pd.DataFrame:
        with self.connect() as con:
            return pd.read_sql_query(
                "SELECT * FROM observations ORDER BY timestamp, symbol", con
            )

    def latest_frame(self) -> pd.DataFrame:
        with self.connect() as con:
            return pd.read_sql_query(
                """
                SELECT o.*
                FROM observations o
                JOIN (
                    SELECT r.run_id
                    FROM scan_runs r
                    WHERE EXISTS (
                        SELECT 1
                        FROM observations x
                        WHERE x.run_id = r.run_id
                    )
                    ORDER BY r.timestamp DESC
                    LIMIT 1
                ) latest ON latest.run_id = o.run_id
                ORDER BY o.rank
                """,
                con,
            )

    def register_learning_events(
        self,
        result: ScanResult,
        horizon_hours: float,
        track_top_n: int,
        gap_minutes: int,
    ) -> int:
        """Register prospective examples, including negatives, without look-ahead."""
        if not result.all_observations:
            return 0
        inserted = 0
        gap_minutes = max(1, int(gap_minutes))
        if int(track_top_n) <= 0:
            selected = result.all_observations
        else:
            selected = result.all_observations[: max(1, int(track_top_n))]
        with self.connect() as con:
            for candidate in selected:
                signal_time = _utc(candidate.timestamp)
                epoch_minute = int(signal_time.timestamp() // 60)
                bucket_minute = epoch_minute - (epoch_minute % gap_minutes)
                bucket_time = datetime.fromtimestamp(bucket_minute * 60, tz=timezone.utc)
                event_key = (
                    f"{candidate.symbol}:{float(horizon_hours):g}:"
                    f"{bucket_time.isoformat()}"
                )
                due_at = signal_time + timedelta(hours=float(horizon_hours))
                cursor = con.execute(
                    """
                    INSERT OR IGNORE INTO learning_events
                    (event_key, source_run_id, signal_timestamp, due_at, horizon_hours,
                     symbol, entry_price, signal_score, risk_score, source_rank,
                     features_json, reasons_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        result.run_id,
                        signal_time.isoformat(),
                        due_at.isoformat(),
                        float(horizon_hours),
                        candidate.symbol,
                        candidate.price,
                        candidate.signal_score,
                        candidate.risk_score,
                        candidate.rank,
                        json.dumps(candidate.features, ensure_ascii=False),
                        json.dumps(candidate.reasons, ensure_ascii=False),
                    ),
                )
                inserted += int(cursor.rowcount > 0)
        return inserted

    def active_learning_symbols(self, limit: int = 30) -> list[str]:
        """Unique pending symbols, prioritized by earliest due event and stale bars."""
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT e.symbol, MIN(e.due_at) AS first_due,
                       MAX(b.timestamp) AS latest_bar
                FROM learning_events e
                LEFT JOIN market_bars b ON b.symbol = e.symbol
                WHERE e.status = 'pending'
                GROUP BY e.symbol
                ORDER BY CASE WHEN latest_bar IS NULL THEN 0 ELSE 1 END,
                         latest_bar, first_due
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def due_learning_events(
        self,
        now: datetime | None = None,
        limit: int = 30,
        horizon_hours: float | None = None,
    ) -> list[dict[str, Any]]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        query = (
            "SELECT * FROM learning_events "
            "WHERE status = 'pending' AND due_at <= ?"
        )
        params: list[Any] = [now.isoformat()]
        if horizon_hours is not None:
            query += " AND ABS(horizon_hours - ?) < 0.0001"
            params.append(float(horizon_hours))
        query += " ORDER BY due_at, id LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connect() as con:
            rows = con.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def mark_learning_attempt(self, event_id: int, error_text: str | None = None) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE learning_events
                SET attempt_count = attempt_count + 1,
                    last_attempt_at = ?,
                    error_text = ?
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), error_text, event_id),
            )

    def finalize_learning_event(
        self,
        event_id: int,
        max_high: float,
        min_low: float,
        final_price: float,
        max_return: float,
        max_drawdown: float,
        bars_found: int,
        thresholds: tuple[float, ...],
        data_quality: str = "usable",
    ) -> None:
        labels = {
            f"{threshold:.6f}": int(max_return >= threshold)
            for threshold in thresholds
        }
        with self.connect() as con:
            con.execute(
                """
                UPDATE learning_events
                SET status = 'finalized', finalized_at = ?,
                    max_high = ?, min_low = ?, final_price = ?,
                    max_return = ?, max_drawdown = ?, bars_found = ?,
                    data_quality = ?, labels_json = ?, error_text = NULL
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    max_high,
                    min_low,
                    final_price,
                    max_return,
                    max_drawdown,
                    int(bars_found),
                    data_quality,
                    json.dumps(labels, ensure_ascii=False),
                    event_id,
                ),
            )

    def learning_events_frame(
        self, status: str | None = None, horizon_hours: float | None = None
    ) -> pd.DataFrame:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if horizon_hours is not None:
            clauses.append("ABS(horizon_hours - ?) < 0.0001")
            params.append(float(horizon_hours))
        query = "SELECT * FROM learning_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY signal_timestamp"
        with self.connect() as con:
            return pd.read_sql_query(query, con, params=tuple(params))

    def learning_counts(self, horizon_hours: float | None = None) -> dict[str, int]:
        query = "SELECT status, COUNT(*) AS count FROM learning_events"
        params: tuple[Any, ...] = ()
        if horizon_hours is not None:
            query += " WHERE ABS(horizon_hours - ?) < 0.0001"
            params = (float(horizon_hours),)
        query += " GROUP BY status"
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
        counts = {"pending": 0, "finalized": 0, "error": 0, "total": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
            counts["total"] += int(row["count"])
        return counts

    def target_counts(
        self, horizon_hours: float, thresholds: tuple[float, ...]
    ) -> list[dict[str, Any]]:
        frame = self.learning_events_frame(
            status="finalized", horizon_hours=horizon_hours
        )
        usable = frame[frame.get("data_quality", "usable") == "usable"] if not frame.empty else frame
        rows: list[dict[str, Any]] = []
        for threshold in thresholds:
            positives = 0
            if not usable.empty:
                positives = int((pd.to_numeric(usable["max_return"], errors="coerce") >= threshold).sum())
            rows.append(
                {
                    "threshold": float(threshold),
                    "rows": int(len(usable)),
                    "positives": positives,
                    "negative": int(len(usable) - positives),
                }
            )
        return rows

    def finalized_since(
        self, timestamp: str | None, horizon_hours: float | None = None
    ) -> int:
        clauses = ["status = 'finalized'"]
        params: list[Any] = []
        if timestamp:
            clauses.append("finalized_at > ?")
            params.append(timestamp)
        if horizon_hours is not None:
            clauses.append("ABS(horizon_hours - ?) < 0.0001")
            params.append(float(horizon_hours))
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS count FROM learning_events WHERE "
                + " AND ".join(clauses),
                tuple(params),
            ).fetchone()
        return int(row["count"] if row else 0)

    def record_model_run(self, metrics: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO model_runs
                (trained_at, horizon_hours, threshold_return, rows, positives,
                 test_rows, average_precision, brier_score, precision_at_0_5,
                 recall_at_0_5, incumbent_average_precision,
                 incumbent_brier_score, accepted, reason, model_path, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metrics["trained_at"],
                    metrics["horizon_hours"],
                    metrics["threshold_return"],
                    metrics.get("rows", 0),
                    metrics.get("positives", 0),
                    metrics.get("test_rows", 0),
                    metrics.get("average_precision"),
                    metrics.get("brier_score"),
                    metrics.get("precision_at_0_5"),
                    metrics.get("recall_at_0_5"),
                    metrics.get("incumbent_average_precision"),
                    metrics.get("incumbent_brier_score"),
                    1 if metrics.get("accepted") else 0,
                    metrics.get("reason", ""),
                    metrics.get("model_path", ""),
                    json.dumps(metrics, ensure_ascii=False),
                ),
            )

    def latest_model_run(
        self,
        horizon_hours: float,
        threshold_return: float,
        accepted_only: bool = False,
    ) -> dict[str, Any] | None:
        where = (
            "WHERE ABS(horizon_hours - ?) < 0.0001 "
            "AND ABS(threshold_return - ?) < 0.000001"
        )
        if accepted_only:
            where += " AND accepted = 1"
        with self.connect() as con:
            row = con.execute(
                f"SELECT * FROM model_runs {where} "
                "ORDER BY trained_at DESC, id DESC LIMIT 1",
                (float(horizon_hours), float(threshold_return)),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["metrics"] = json.loads(result.get("metrics_json") or "{}")
        except json.JSONDecodeError:
            result["metrics"] = {}
        return result

    def model_runs_frame(self) -> pd.DataFrame:
        with self.connect() as con:
            return pd.read_sql_query(
                "SELECT * FROM model_runs ORDER BY trained_at DESC, id DESC", con
            )

    def learning_status(
        self, horizon_hours: float, thresholds: tuple[float, ...]
    ) -> dict[str, Any]:
        models: list[dict[str, Any]] = []
        for target in self.target_counts(horizon_hours, thresholds):
            latest = self.latest_model_run(
                horizon_hours, target["threshold"], accepted_only=True
            )
            target["model"] = latest
            models.append(target)
        return {
            "counts": self.learning_counts(horizon_hours),
            "targets": models,
        }
