from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from spike_scanner.models import Candidate, ScanResult
from spike_scanner.providers.base import MarketDataProvider
from spike_scanner.scoring import market_phase_at
from spike_scanner.storage import Storage

logger = logging.getLogger(__name__)
MODULE_VERSION = "2.0.0-phase1"


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _as_ratio(value: Any, default: float = 0.0) -> float:
    """Normalize percent-like inputs to a decimal ratio.

    The scanner normally stores 0.25 for +25 %. Some providers or imported
    files use 25 instead. Values above 5 are therefore interpreted as percent.
    """
    number = _as_float(value, default)
    if abs(number) > 5.0:
        number /= 100.0
    return number


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _thresholds(value: str | None) -> tuple[float, ...]:
    if not value:
        return (0.10, 0.20, 0.50, 1.00)
    parsed: list[float] = []
    for raw in value.replace(";", ",").split(","):
        raw = raw.strip().replace("%", "")
        if not raw:
            continue
        number = float(raw.replace(",", "."))
        if number > 1:
            number /= 100.0
        if 0 < number <= 5:
            parsed.append(round(number, 6))
    return tuple(sorted(set(parsed))) or (0.10, 0.20, 0.50, 1.00)


@dataclass(slots=True)
class TopMoverLearningConfig:
    enabled: bool = True
    database_path: Path = Path("data/top_mover_learning.sqlite")
    horizon_hours: float = 24.0
    top_movers_per_scan: int = 10
    controls_per_scan: int = 5
    event_gap_minutes: int = 120
    min_price: float = 0.20
    max_price: float = 10.00
    min_change_ratio: float = 0.10
    min_relative_volume: float = 2.0
    stop_pct: float = 0.07
    targets: tuple[float, ...] = (0.10, 0.20, 0.50, 1.00)
    finalize_per_scan: int = 25
    refresh_symbols_per_scan: int = 25
    min_bars_per_event: int = 8
    max_attempts: int = 8

    @classmethod
    def from_env(cls, settings: Any | None = None) -> "TopMoverLearningConfig":
        settings_min_price = _as_float(getattr(settings, "min_price", 0.20), 0.20)
        return cls(
            enabled=_as_bool(os.getenv("TOP_MOVER_LEARNING_ENABLED"), True),
            database_path=Path(
                os.getenv(
                    "TOP_MOVER_LEARNING_DATABASE_PATH",
                    "data/top_mover_learning.sqlite",
                )
            ),
            horizon_hours=float(os.getenv("TOP_MOVER_LEARNING_HORIZON_HOURS", "24")),
            top_movers_per_scan=int(os.getenv("TOP_MOVER_LEARNING_TOP_N", "10")),
            controls_per_scan=int(os.getenv("TOP_MOVER_LEARNING_CONTROLS", "5")),
            event_gap_minutes=int(os.getenv("TOP_MOVER_LEARNING_GAP_MINUTES", "120")),
            min_price=float(os.getenv("TOP_MOVER_LEARNING_MIN_PRICE", str(settings_min_price))),
            max_price=float(os.getenv("TOP_MOVER_LEARNING_MAX_PRICE", "10.00")),
            min_change_ratio=_as_ratio(
                os.getenv("TOP_MOVER_LEARNING_MIN_CHANGE", "0.10"), 0.10
            ),
            min_relative_volume=float(
                os.getenv("TOP_MOVER_LEARNING_MIN_RVOL", "2.0")
            ),
            stop_pct=_as_ratio(os.getenv("TOP_MOVER_LEARNING_STOP_PCT", "0.07"), 0.07),
            targets=_thresholds(os.getenv("TOP_MOVER_LEARNING_TARGETS")),
            finalize_per_scan=int(os.getenv("TOP_MOVER_LEARNING_FINALIZE_PER_SCAN", "25")),
            refresh_symbols_per_scan=int(os.getenv("TOP_MOVER_LEARNING_REFRESH_SYMBOLS", "25")),
            min_bars_per_event=int(os.getenv("TOP_MOVER_LEARNING_MIN_BARS", "8")),
            max_attempts=int(os.getenv("TOP_MOVER_LEARNING_MAX_ATTEMPTS", "8")),
        )


@dataclass(slots=True)
class TopMoverLearningSummary:
    registered_top_movers: int = 0
    registered_controls: int = 0
    refreshed_symbols: int = 0
    bars_stored: int = 0
    due_checked: int = 0
    finalized: int = 0
    limited_finalized: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TopMoverLearningStore:
    """Separate forward-learning database for momentum top movers.

    A dedicated file keeps the new research data independent from scanner.db
    and from the paper-trading database. Existing scanner data is never
    rewritten by this module.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
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
                CREATE TABLE IF NOT EXISTS top_mover_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    source_run_id TEXT NOT NULL,
                    signal_timestamp TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    horizon_hours REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    cohort TEXT NOT NULL,
                    top_mover_rank INTEGER NOT NULL,
                    source_rank INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_pct REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    signal_score REAL NOT NULL,
                    risk_score REAL NOT NULL,
                    change_ratio REAL NOT NULL,
                    relative_volume REAL NOT NULL,
                    spread_pct REAL NOT NULL,
                    dollar_volume_5m REAL NOT NULL,
                    features_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    finalized_at TEXT,
                    bars_found INTEGER NOT NULL DEFAULT 0,
                    data_quality TEXT NOT NULL DEFAULT 'pending',
                    max_high REAL,
                    min_low REAL,
                    final_price REAL,
                    max_return REAL,
                    max_drawdown REAL,
                    first_stop_at TEXT,
                    first_event_kind TEXT,
                    first_event_at TEXT,
                    outcome TEXT,
                    target_hits_json TEXT NOT NULL DEFAULT '{}',
                    path_labels_json TEXT NOT NULL DEFAULT '{}',
                    horizon_returns_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_top_mover_cases_due
                ON top_mover_cases(status, due_at);

                CREATE INDEX IF NOT EXISTS idx_top_mover_cases_symbol_time
                ON top_mover_cases(symbol, signal_timestamp);

                CREATE INDEX IF NOT EXISTS idx_top_mover_cases_cohort_phase
                ON top_mover_cases(cohort, phase, status);
                """
            )

    def register_cases(
        self,
        result: ScanResult,
        config: TopMoverLearningConfig,
    ) -> tuple[int, int]:
        if not result.all_observations:
            return 0, 0

        eligible: list[Candidate] = []
        controls: list[Candidate] = []
        for candidate in result.all_observations:
            features = candidate.features or {}
            price = _as_float(candidate.price)
            change = _as_ratio(features.get("screener_change_ratio"))
            rvol = max(
                _as_float(features.get("screener_relative_volume")),
                _as_float(features.get("relative_volume_5m")),
            )
            if not (config.min_price <= price <= config.max_price):
                continue
            if change >= config.min_change_ratio and rvol >= config.min_relative_volume:
                eligible.append(candidate)
            else:
                controls.append(candidate)

        def ranking_key(candidate: Candidate) -> tuple[float, float, float, float, float]:
            features = candidate.features or {}
            return (
                _as_ratio(features.get("screener_change_ratio")),
                max(
                    _as_float(features.get("screener_relative_volume")),
                    _as_float(features.get("relative_volume_5m")),
                ),
                _as_float(features.get("dollar_volume_5m")),
                _as_float(candidate.signal_score),
                -_as_float(candidate.risk_score),
            )

        eligible.sort(key=ranking_key, reverse=True)
        controls.sort(key=ranking_key, reverse=True)
        selected = [
            (candidate, "TOP_MOVER")
            for candidate in eligible[: max(0, config.top_movers_per_scan)]
        ]
        selected.extend(
            (candidate, "CONTROL")
            for candidate in controls[: max(0, config.controls_per_scan)]
        )

        top_inserted = 0
        control_inserted = 0
        gap_minutes = max(1, int(config.event_gap_minutes))
        with self.connect() as con:
            for top_rank, (candidate, cohort) in enumerate(selected, start=1):
                signal_time = _utc(candidate.timestamp or result.timestamp)
                epoch_minute = int(signal_time.timestamp() // 60)
                bucket_minute = epoch_minute - (epoch_minute % gap_minutes)
                bucket_time = datetime.fromtimestamp(bucket_minute * 60, tz=timezone.utc)
                event_key = (
                    f"{candidate.symbol.upper()}:{config.horizon_hours:g}:"
                    f"{bucket_time.isoformat()}"
                )
                features = candidate.features or {}
                change = _as_ratio(features.get("screener_change_ratio"))
                rvol = max(
                    _as_float(features.get("screener_relative_volume")),
                    _as_float(features.get("relative_volume_5m")),
                )
                entry = _as_float(candidate.price)
                stop_price = entry * (1.0 - config.stop_pct)
                cursor = con.execute(
                    """
                    INSERT OR IGNORE INTO top_mover_cases
                    (event_key, source_run_id, signal_timestamp, due_at,
                     horizon_hours, symbol, phase, cohort, top_mover_rank,
                     source_rank, entry_price, stop_pct, stop_price,
                     signal_score, risk_score, change_ratio, relative_volume,
                     spread_pct, dollar_volume_5m, features_json, reasons_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        result.run_id,
                        signal_time.isoformat(),
                        (signal_time + timedelta(hours=config.horizon_hours)).isoformat(),
                        float(config.horizon_hours),
                        candidate.symbol.upper(),
                        market_phase_at(signal_time),
                        cohort,
                        top_rank,
                        int(candidate.rank or 0),
                        entry,
                        float(config.stop_pct),
                        stop_price,
                        _as_float(candidate.signal_score),
                        _as_float(candidate.risk_score),
                        change,
                        rvol,
                        _as_ratio(features.get("spread_pct")),
                        _as_float(features.get("dollar_volume_5m")),
                        json.dumps(features, ensure_ascii=False),
                        json.dumps(candidate.reasons or [], ensure_ascii=False),
                    ),
                )
                if cursor.rowcount > 0:
                    if cohort == "TOP_MOVER":
                        top_inserted += 1
                    else:
                        control_inserted += 1
        return top_inserted, control_inserted

    def active_symbols(self, limit: int) -> list[str]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT symbol, MIN(due_at) AS first_due
                FROM top_mover_cases
                WHERE status = 'pending'
                GROUP BY symbol
                ORDER BY first_due
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def due_cases(self, limit: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT * FROM top_mover_cases
                WHERE status = 'pending' AND due_at <= ?
                ORDER BY due_at, id
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_attempt(self, case_id: int, error_text: str) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE top_mover_cases
                SET attempt_count = attempt_count + 1,
                    last_attempt_at = ?, error_text = ?
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), error_text, int(case_id)),
            )

    def finalize_case(
        self,
        case_id: int,
        *,
        bars_found: int,
        data_quality: str,
        max_high: float,
        min_low: float,
        final_price: float,
        max_return: float,
        max_drawdown: float,
        first_stop_at: str | None,
        first_event_kind: str,
        first_event_at: str | None,
        outcome: str,
        target_hits: dict[str, Any],
        path_labels: dict[str, Any],
        horizon_returns: dict[str, Any],
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE top_mover_cases
                SET status = 'finalized', finalized_at = ?, bars_found = ?,
                    data_quality = ?, max_high = ?, min_low = ?, final_price = ?,
                    max_return = ?, max_drawdown = ?, first_stop_at = ?,
                    first_event_kind = ?, first_event_at = ?, outcome = ?,
                    target_hits_json = ?, path_labels_json = ?,
                    horizon_returns_json = ?, error_text = NULL
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    int(bars_found),
                    data_quality,
                    max_high,
                    min_low,
                    final_price,
                    max_return,
                    max_drawdown,
                    first_stop_at,
                    first_event_kind,
                    first_event_at,
                    outcome,
                    json.dumps(target_hits, ensure_ascii=False),
                    json.dumps(path_labels, ensure_ascii=False),
                    json.dumps(horizon_returns, ensure_ascii=False),
                    int(case_id),
                ),
            )

    def summary(self) -> dict[str, Any]:
        with self.connect() as con:
            status_rows = con.execute(
                """
                SELECT cohort, status, COUNT(*) AS count
                FROM top_mover_cases
                GROUP BY cohort, status
                """
            ).fetchall()
            phase_rows = con.execute(
                """
                SELECT phase, cohort, COUNT(*) AS count
                FROM top_mover_cases
                GROUP BY phase, cohort
                """
            ).fetchall()
            finalized = con.execute(
                """
                SELECT cohort, phase, path_labels_json
                FROM top_mover_cases
                WHERE status = 'finalized' AND data_quality = 'usable'
                """
            ).fetchall()

        status: dict[str, dict[str, int]] = {}
        for row in status_rows:
            status.setdefault(str(row["cohort"]), {})[str(row["status"])] = int(row["count"])
        phases: dict[str, dict[str, int]] = {}
        for row in phase_rows:
            phases.setdefault(str(row["phase"]), {})[str(row["cohort"])] = int(row["count"])

        targets: dict[str, dict[str, int]] = {}
        for row in finalized:
            try:
                labels = json.loads(row["path_labels_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                labels = {}
            for key, value in labels.items():
                bucket = targets.setdefault(
                    key,
                    {
                        "target_before_stop": 0,
                        "stop_before_target": 0,
                        "ambiguous": 0,
                        "no_event": 0,
                    },
                )
                result = str((value or {}).get("result") or "no_event")
                if result in bucket:
                    bucket[result] += 1
                else:
                    bucket["no_event"] += 1
        return {"status": status, "phases": phases, "targets": targets}

    def recent_cases(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT * FROM top_mover_cases
                ORDER BY signal_timestamp DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_csv(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            frame = pd.read_sql_query(
                "SELECT * FROM top_mover_cases ORDER BY signal_timestamp, id",
                con,
            )
        frame.to_csv(path, index=False)
        return path


def _first_timestamp(frame: pd.DataFrame, mask: pd.Series) -> str | None:
    matches = frame.loc[mask, "timestamp"]
    if matches.empty:
        return None
    value = pd.Timestamp(matches.iloc[0])
    return value.isoformat()


def _minutes_between(start: datetime, end_value: str | None) -> float | None:
    if not end_value:
        return None
    return round((_utc(end_value) - start).total_seconds() / 60.0, 2)


def evaluate_path(
    frame: pd.DataFrame,
    *,
    signal_time: datetime,
    entry_price: float,
    stop_price: float,
    targets: Iterable[float],
) -> dict[str, Any]:
    """Evaluate target-before-stop paths conservatively.

    If stop and target are touched inside the same minute candle before either
    was touched in an earlier candle, the result is marked ambiguous. This
    avoids inventing an intrabar order that the data cannot prove.
    """
    working = frame.copy().sort_values("timestamp").reset_index(drop=True)
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close"):
        working[column] = pd.to_numeric(working[column], errors="coerce")
    working = working.dropna(subset=["timestamp", "high", "low", "close"])
    if working.empty:
        raise ValueError("Keine gültigen Kursbalken im Auswertungsfenster")

    stop_mask = working["low"] <= float(stop_price)
    first_stop_at = _first_timestamp(working, stop_mask)
    target_hits: dict[str, Any] = {}
    path_labels: dict[str, Any] = {}

    for threshold in targets:
        target_price = entry_price * (1.0 + float(threshold))
        key = f"{float(threshold):.6f}"
        target_at: str | None = None
        stop_at: str | None = None
        result = "no_event"
        label: int | None = None

        for row in working.itertuples(index=False):
            timestamp = pd.Timestamp(row.timestamp).isoformat()
            target_hit = float(row.high) >= target_price
            stop_hit = float(row.low) <= stop_price
            if target_hit and stop_hit:
                target_at = timestamp
                stop_at = timestamp
                result = "ambiguous"
                label = -1
                break
            if target_hit:
                target_at = timestamp
                result = "target_before_stop"
                label = 1
                break
            if stop_hit:
                stop_at = timestamp
                result = "stop_before_target"
                label = 0
                break

        target_hits[key] = target_at
        path_labels[key] = {
            "threshold": float(threshold),
            "target_price": round(target_price, 6),
            "label": label,
            "result": result,
            "target_at": target_at,
            "stop_at": stop_at,
            "time_to_target_minutes": _minutes_between(signal_time, target_at),
            "time_to_stop_minutes": _minutes_between(signal_time, stop_at),
        }

    first_key = f"{min(float(x) for x in targets):.6f}"
    first_result = path_labels.get(first_key, {})
    first_kind = str(first_result.get("result") or "no_event")
    first_at = first_result.get("target_at") or first_result.get("stop_at")

    horizon_returns: dict[str, Any] = {}
    for hours in (1, 4, 24):
        cutoff = signal_time + timedelta(hours=hours)
        available = working[working["timestamp"] <= cutoff]
        if available.empty:
            horizon_returns[str(hours)] = None
        else:
            close = float(available["close"].iloc[-1])
            horizon_returns[str(hours)] = round(close / entry_price - 1.0, 8)

    max_high = float(working["high"].max())
    min_low = float(working["low"].min())
    final_price = float(working["close"].iloc[-1])
    return {
        "bars_found": int(len(working)),
        "max_high": max_high,
        "min_low": min_low,
        "final_price": final_price,
        "max_return": max_high / entry_price - 1.0,
        "max_drawdown": min_low / entry_price - 1.0,
        "first_stop_at": first_stop_at,
        "first_event_kind": first_kind,
        "first_event_at": first_at,
        "outcome": first_kind,
        "target_hits": target_hits,
        "path_labels": path_labels,
        "horizon_returns": horizon_returns,
    }


class TopMoverLearningEngine:
    def __init__(
        self,
        settings: Any,
        provider: MarketDataProvider,
        storage: Storage,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.storage = storage
        self.config = TopMoverLearningConfig.from_env(settings)
        self.store = TopMoverLearningStore(self.config.database_path)

    def run(
        self,
        result: ScanResult | None = None,
        captured_symbols: set[str] | None = None,
    ) -> TopMoverLearningSummary:
        summary = TopMoverLearningSummary()
        if not self.config.enabled:
            return summary

        if result is not None:
            top_count, control_count = self.store.register_cases(result, self.config)
            summary.registered_top_movers = top_count
            summary.registered_controls = control_count

        self._refresh_pending(summary, captured_symbols or set())
        self._finalize_due(summary)
        return summary

    def _refresh_pending(
        self,
        summary: TopMoverLearningSummary,
        captured_symbols: set[str],
    ) -> None:
        captured = {str(symbol).upper() for symbol in captured_symbols}
        count = max(
            int(getattr(self.settings, "bar_count", 120)),
            int(self.config.horizon_hours * 60 * 1.6) + 300,
        )
        for symbol in self.store.active_symbols(self.config.refresh_symbols_per_scan):
            if symbol.upper() in captured:
                continue
            try:
                bars = self.provider.get_bars(symbol, count)
                summary.bars_stored += self.storage.save_market_bars(symbol, bars)
                summary.refreshed_symbols += 1
            except Exception as exc:
                summary.warnings.append(
                    f"{symbol}: Top-Mover-Nachverfolgung vorübergehend nicht möglich ({exc})"
                )

    def _finalize_due(self, summary: TopMoverLearningSummary) -> None:
        due = self.store.due_cases(self.config.finalize_per_scan)
        summary.due_checked = len(due)
        history_count = max(
            int(getattr(self.settings, "bar_count", 120)),
            int(self.config.horizon_hours * 60 * 1.6) + 300,
        )
        for case in due:
            case_id = int(case["id"])
            try:
                window = self.storage.market_bars_frame(
                    str(case["symbol"]),
                    str(case["signal_timestamp"]),
                    str(case["due_at"]),
                )
                if len(window) < self.config.min_bars_per_event:
                    try:
                        bars = self.provider.get_bars(str(case["symbol"]), history_count)
                        self.storage.save_market_bars(str(case["symbol"]), bars)
                        window = self.storage.market_bars_frame(
                            str(case["symbol"]),
                            str(case["signal_timestamp"]),
                            str(case["due_at"]),
                        )
                    except Exception:
                        pass

                if window.empty:
                    raise ValueError("Keine Kursdaten im 24-Stunden-Fenster")

                attempts = int(case.get("attempt_count") or 0)
                if (
                    len(window) < self.config.min_bars_per_event
                    and attempts < self.config.max_attempts
                ):
                    raise ValueError(
                        f"Erst {len(window)} Kursbalken verfügbar; später erneut prüfen"
                    )

                evaluation = evaluate_path(
                    window,
                    signal_time=_utc(str(case["signal_timestamp"])),
                    entry_price=float(case["entry_price"]),
                    stop_price=float(case["stop_price"]),
                    targets=self.config.targets,
                )
                quality = (
                    "usable"
                    if evaluation["bars_found"] >= self.config.min_bars_per_event
                    else "limited"
                )
                self.store.finalize_case(
                    case_id,
                    data_quality=quality,
                    **evaluation,
                )
                summary.finalized += 1
                if quality != "usable":
                    summary.limited_finalized += 1
            except Exception as exc:
                message = f"{case['symbol']}: Top-Mover-Fall noch nicht auswertbar ({exc})"
                self.store.mark_attempt(case_id, message)
                summary.warnings.append(message)
