from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from spike_scanner.models import Candidate, ScanResult
from spike_scanner.scoring import (
    execution_quality_score,
    market_phase_at,
    purchase_assessment,
    signal_age_minutes,
)

logger = logging.getLogger(__name__)


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip().replace(",", "."))


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _et_zone():
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return timezone.utc


def _et_date(value: str | datetime) -> str:
    return _utc(value).astimezone(_et_zone()).date().isoformat()


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


@dataclass(slots=True)
class PilotAssessment:
    score: float
    quality: str
    eligible: bool
    blockers: list[str]
    execution_quality: float
    mover_change_pct: float
    mover_relative_volume: float


@dataclass(slots=True)
class EntryGuardResult:
    allowed: bool
    result: str
    reason: str
    scan_reference_price: float
    refreshed_entry_price: float | None
    drift_pct: float | None
    quote_at: str | None
    quote_age_seconds: float | None
    spread_pct: float | None
    best_bid: float | None
    best_ask: float | None
    source: str
    notes: list[str]


@dataclass(slots=True)
class PaperTradingConfig:
    """Configuration for the internal simulation only.

    This module never connects to a broker and never sends an order.
    """

    mode: str = "off"  # off | shadow | paper
    profile: str = "standard"  # standard | top_mover_pilot
    database_path: Path = Path("data/papertrades.sqlite")
    min_entry_price: float = 0.20
    max_entry_price: float = 10.00
    max_open_positions: int = 1
    max_new_trades_per_day: int = 2
    max_open_positions_pre: int = 1
    max_open_positions_rth: int = 1
    max_open_positions_ath: int = 1
    min_purchase_score: float = 65.0
    max_signal_age_minutes: float = 30.0
    stop_loss_pct: float = 0.05
    target_pct: float = 0.10
    max_hold_hours: float = 24.0
    slippage_pct: float = 0.0025
    monitor_bar_count: int = 600
    cooldown_hours: float = 24.0

    # Top-Mover-Pilot: bewusst lockerer als die Standard-Kaufbewertung,
    # aber weiterhin mit Preis-, Spread-, LiquiditÃ¤ts- und Risikogrenzen.
    top_mover_min_change_pct: float = 0.10
    top_mover_min_relative_volume: float = 2.0
    top_mover_min_signal_score: float = 35.0
    top_mover_max_risk_score: float = 75.0
    top_mover_max_spread_rth: float = 0.04
    top_mover_max_spread_extended: float = 0.03
    top_mover_min_dollar_volume_5m: float = 50_000.0
    top_mover_candidates_per_scan: int = 10

    # Entry-Quality-Guard: kurz vor einem simulierten Einstieg werden
    # Kursfrische, Kursdrift und aktueller Spread erneut geprÃ¼ft.
    entry_guard_enabled: bool = True
    entry_guard_bar_count: int = 5
    entry_guard_max_quote_age_seconds: float = 120.0
    entry_guard_max_chase_pct: float = 0.03
    entry_guard_max_breakdown_pct: float = 0.015

    @classmethod
    def from_env(cls, scanner_settings: Any) -> "PaperTradingConfig":
        mode = os.getenv("PAPERTRADING_MODE", "off").strip().lower()
        if mode not in {"off", "shadow", "paper"}:
            mode = "off"

        profile = os.getenv("PAPERTRADING_PROFILE", "standard").strip().lower()
        if profile not in {"standard", "top_mover_pilot"}:
            profile = "standard"

        pilot = profile == "top_mover_pilot"
        return cls(
            mode=mode,
            profile=profile,
            database_path=Path(os.getenv("PAPER_DATABASE_PATH", "data/papertrades.sqlite")),
            min_entry_price=_as_float(
                "PAPER_MIN_ENTRY_PRICE", float(getattr(scanner_settings, "min_price", 0.20))
            ),
            max_entry_price=_as_float("PAPER_MAX_ENTRY_PRICE", 10.00),
            max_open_positions=max(1, _as_int("PAPER_MAX_OPEN_POSITIONS", 2 if pilot else 1)),
            max_new_trades_per_day=max(
                1, _as_int("PAPER_MAX_NEW_TRADES_PER_DAY", 8 if pilot else 2)
            ),
            max_open_positions_pre=max(
                1, _as_int("PAPER_MAX_OPEN_POSITIONS_PRE", 2 if pilot else 1)
            ),
            max_open_positions_rth=max(
                1, _as_int("PAPER_MAX_OPEN_POSITIONS_RTH", 4 if pilot else 1)
            ),
            max_open_positions_ath=max(
                1, _as_int("PAPER_MAX_OPEN_POSITIONS_ATH", 2 if pilot else 1)
            ),
            min_purchase_score=_as_float(
                "PAPER_MIN_PURCHASE_SCORE", 55.0 if pilot else 65.0
            ),
            max_signal_age_minutes=_as_float(
                "PAPER_MAX_SIGNAL_AGE_MINUTES", 15.0 if pilot else 30.0
            ),
            stop_loss_pct=_as_float("PAPER_STOP_LOSS_PCT", 0.07 if pilot else 0.05),
            target_pct=_as_float("PAPER_TARGET_PCT", 0.10),
            max_hold_hours=_as_float("PAPER_MAX_HOLD_HOURS", 24.0),
            slippage_pct=_as_float("PAPER_SLIPPAGE_PCT", 0.0025),
            monitor_bar_count=max(120, _as_int("PAPER_MONITOR_BAR_COUNT", 600)),
            cooldown_hours=_as_float("PAPER_COOLDOWN_HOURS", 4.0 if pilot else 24.0),
            top_mover_min_change_pct=_as_float("PAPER_TOP_MOVER_MIN_CHANGE_PCT", 0.10),
            top_mover_min_relative_volume=_as_float(
                "PAPER_TOP_MOVER_MIN_RELATIVE_VOLUME", 2.0
            ),
            top_mover_min_signal_score=_as_float(
                "PAPER_TOP_MOVER_MIN_SIGNAL_SCORE", 35.0
            ),
            top_mover_max_risk_score=_as_float(
                "PAPER_TOP_MOVER_MAX_RISK_SCORE", 75.0
            ),
            top_mover_max_spread_rth=_as_float(
                "PAPER_TOP_MOVER_MAX_SPREAD_RTH", 0.04
            ),
            top_mover_max_spread_extended=_as_float(
                "PAPER_TOP_MOVER_MAX_SPREAD_EXTENDED", 0.03
            ),
            top_mover_min_dollar_volume_5m=_as_float(
                "PAPER_TOP_MOVER_MIN_DOLLAR_VOLUME_5M", 50_000.0
            ),
            top_mover_candidates_per_scan=max(
                1, _as_int("PAPER_TOP_MOVER_CANDIDATES_PER_SCAN", 10)
            ),
            entry_guard_enabled=_as_bool("PAPER_ENTRY_GUARD_ENABLED", True),
            entry_guard_bar_count=max(
                2, _as_int("PAPER_ENTRY_GUARD_BAR_COUNT", 5)
            ),
            entry_guard_max_quote_age_seconds=max(
                15.0, _as_float("PAPER_ENTRY_GUARD_MAX_QUOTE_AGE_SECONDS", 120.0)
            ),
            entry_guard_max_chase_pct=max(
                0.0, _as_float("PAPER_ENTRY_GUARD_MAX_CHASE_PCT", 0.03)
            ),
            entry_guard_max_breakdown_pct=max(
                0.0, _as_float("PAPER_ENTRY_GUARD_MAX_BREAKDOWN_PCT", 0.015)
            ),
        )

    @property
    def enabled(self) -> bool:
        return self.mode in {"shadow", "paper"}

    def max_open_for_phase(self, phase: str) -> int:
        phase_code = str(phase or "OFF").upper()
        phase_limit = {
            "PRE": self.max_open_positions_pre,
            "RTH": self.max_open_positions_rth,
            "ATH": self.max_open_positions_ath,
        }.get(phase_code, 0)
        return min(self.max_open_positions, max(0, int(phase_limit)))


class PaperTradingStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _initialize(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_key TEXT NOT NULL UNIQUE,
                    mode TEXT NOT NULL,
                    strategy_profile TEXT NOT NULL DEFAULT 'standard',
                    status TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    source_recommendation_at TEXT,
                    phase TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    opened_trade_date_et TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    entry_reference_price REAL NOT NULL,
                    entry_fill_price REAL NOT NULL,
                    entry_spread_pct REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    signal_score REAL NOT NULL,
                    risk_score REAL NOT NULL,
                    purchase_score REAL NOT NULL,
                    purchase_quality TEXT NOT NULL,
                    model_probability REAL,
                    mover_change_pct REAL,
                    mover_relative_volume REAL,
                    scan_reference_price REAL,
                    refreshed_entry_price REAL,
                    entry_price_drift_pct REAL,
                    entry_quote_at TEXT,
                    entry_quote_age_seconds REAL,
                    entry_guard_result TEXT NOT NULL DEFAULT 'NOT_RUN',
                    entry_guard_source TEXT,
                    entry_guard_spread_pct REAL,
                    last_bar_at TEXT,
                    last_price REAL,
                    max_high REAL NOT NULL,
                    min_low REAL NOT NULL,
                    max_favorable_pct REAL NOT NULL DEFAULT 0,
                    max_adverse_pct REAL NOT NULL DEFAULT 0,
                    closed_at TEXT,
                    exit_reason TEXT,
                    exit_reference_price REAL,
                    exit_fill_price REAL,
                    gross_return_pct REAL,
                    net_return_pct REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_paper_positions_status
                ON paper_positions(status, opened_at);
                CREATE INDEX IF NOT EXISTS idx_paper_positions_symbol
                ON paper_positions(symbol, opened_at DESC);

                CREATE TABLE IF NOT EXISTS paper_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    strategy_profile TEXT NOT NULL DEFAULT 'standard',
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    price REAL NOT NULL,
                    signal_score REAL NOT NULL,
                    risk_score REAL NOT NULL,
                    purchase_score REAL NOT NULL,
                    model_probability REAL,
                    mover_change_pct REAL,
                    mover_relative_volume REAL,
                    scan_reference_price REAL,
                    refreshed_entry_price REAL,
                    entry_price_drift_pct REAL,
                    entry_quote_at TEXT,
                    entry_quote_age_seconds REAL,
                    entry_guard_result TEXT NOT NULL DEFAULT 'NOT_RUN',
                    entry_guard_source TEXT,
                    entry_guard_spread_pct REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_paper_decisions_time
                ON paper_decisions(created_at DESC);
                """
            )
            self._ensure_column(
                con, "paper_positions", "strategy_profile",
                "TEXT NOT NULL DEFAULT 'standard'"
            )
            self._ensure_column(con, "paper_positions", "mover_change_pct", "REAL")
            self._ensure_column(con, "paper_positions", "mover_relative_volume", "REAL")
            self._ensure_column(con, "paper_positions", "scan_reference_price", "REAL")
            self._ensure_column(con, "paper_positions", "refreshed_entry_price", "REAL")
            self._ensure_column(con, "paper_positions", "entry_price_drift_pct", "REAL")
            self._ensure_column(con, "paper_positions", "entry_quote_at", "TEXT")
            self._ensure_column(con, "paper_positions", "entry_quote_age_seconds", "REAL")
            self._ensure_column(
                con, "paper_positions", "entry_guard_result",
                "TEXT NOT NULL DEFAULT 'NOT_RUN'"
            )
            self._ensure_column(con, "paper_positions", "entry_guard_source", "TEXT")
            self._ensure_column(con, "paper_positions", "entry_guard_spread_pct", "REAL")
            self._ensure_column(
                con, "paper_decisions", "strategy_profile",
                "TEXT NOT NULL DEFAULT 'standard'"
            )
            self._ensure_column(con, "paper_decisions", "mover_change_pct", "REAL")
            self._ensure_column(con, "paper_decisions", "mover_relative_volume", "REAL")
            self._ensure_column(con, "paper_decisions", "scan_reference_price", "REAL")
            self._ensure_column(con, "paper_decisions", "refreshed_entry_price", "REAL")
            self._ensure_column(con, "paper_decisions", "entry_price_drift_pct", "REAL")
            self._ensure_column(con, "paper_decisions", "entry_quote_at", "TEXT")
            self._ensure_column(con, "paper_decisions", "entry_quote_age_seconds", "REAL")
            self._ensure_column(
                con, "paper_decisions", "entry_guard_result",
                "TEXT NOT NULL DEFAULT 'NOT_RUN'"
            )
            self._ensure_column(con, "paper_decisions", "entry_guard_source", "TEXT")
            self._ensure_column(con, "paper_decisions", "entry_guard_spread_pct", "REAL")

    @staticmethod
    def _ensure_column(
        con: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in con.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def open_positions(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY opened_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def has_trade_key(self, trade_key: str) -> bool:
        with self.connect() as con:
            row = con.execute(
                "SELECT 1 FROM paper_positions WHERE trade_key=? LIMIT 1", (trade_key,)
            ).fetchone()
        return row is not None

    def recently_traded(self, symbol: str, since: datetime) -> bool:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT 1 FROM paper_positions
                WHERE symbol=? AND opened_at>=? LIMIT 1
                """,
                (symbol.upper(), since.astimezone(timezone.utc).isoformat()),
            ).fetchone()
        return row is not None

    def opened_count_for_et_date(self, date_et: str) -> int:
        with self.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM paper_positions WHERE opened_trade_date_et=?",
                (date_et,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def open_count_for_phase(self, phase: str) -> int:
        phase_code = str(phase or "OFF").upper()
        with self.connect() as con:
            row = con.execute(
                """
                SELECT COUNT(*) AS n FROM paper_positions
                WHERE status='OPEN' AND UPPER(phase)=?
                """,
                (phase_code,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def open_counts_by_phase(self) -> dict[str, int]:
        counts = {"PRE": 0, "RTH": 0, "ATH": 0, "OFF": 0}
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT UPPER(phase) AS phase_code, COUNT(*) AS n
                FROM paper_positions
                WHERE status='OPEN'
                GROUP BY UPPER(phase)
                """
            ).fetchall()
        for row in rows:
            phase_code = str(row["phase_code"] or "OFF").upper()
            counts[phase_code] = int(row["n"] or 0)
        return counts

    def record_decision(
        self,
        *,
        created_at: str,
        run_id: str,
        candidate: Candidate,
        mode: str,
        decision: str,
        reason: str,
        phase: str,
        purchase_score: float,
        strategy_profile: str = "standard",
        mover_change_pct: float | None = None,
        mover_relative_volume: float | None = None,
        entry_guard: EntryGuardResult | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO paper_decisions
                (created_at, run_id, symbol, mode, strategy_profile, decision, reason,
                 phase, price, signal_score, risk_score, purchase_score,
                 model_probability, mover_change_pct, mover_relative_volume,
                 scan_reference_price, refreshed_entry_price, entry_price_drift_pct,
                 entry_quote_at, entry_quote_age_seconds, entry_guard_result,
                 entry_guard_source, entry_guard_spread_pct, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    run_id,
                    candidate.symbol.upper(),
                    mode,
                    strategy_profile,
                    decision,
                    reason,
                    phase,
                    float(candidate.price),
                    float(candidate.signal_score),
                    float(candidate.risk_score),
                    float(purchase_score),
                    candidate.model_probability,
                    mover_change_pct,
                    mover_relative_volume,
                    float(entry_guard.scan_reference_price) if entry_guard else float(candidate.price),
                    entry_guard.refreshed_entry_price if entry_guard else None,
                    entry_guard.drift_pct if entry_guard else None,
                    entry_guard.quote_at if entry_guard else None,
                    entry_guard.quote_age_seconds if entry_guard else None,
                    entry_guard.result if entry_guard else "NOT_RUN",
                    entry_guard.source if entry_guard else None,
                    entry_guard.spread_pct if entry_guard else None,
                    _safe_json(metadata or {}),
                ),
            )

    def create_position(self, values: dict[str, Any]) -> int:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.connect() as con:
            cur = con.execute(
                f"INSERT INTO paper_positions ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            return int(cur.lastrowid)

    def update_position(self, position_id: int, **values: Any) -> None:
        if not values:
            return
        assignments = ", ".join(f"{column}=?" for column in values)
        with self.connect() as con:
            con.execute(
                f"UPDATE paper_positions SET {assignments} WHERE id=?",
                (*values.values(), int(position_id)),
            )

    def summary(self) -> dict[str, Any]:
        with self.connect() as con:
            open_count = int(
                con.execute("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'").fetchone()[0]
            )
            closed_count = int(
                con.execute("SELECT COUNT(*) FROM paper_positions WHERE status='CLOSED'").fetchone()[0]
            )
            row = con.execute(
                """
                SELECT AVG(net_return_pct) AS avg_return,
                       SUM(CASE WHEN net_return_pct>0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN net_return_pct<=0 THEN 1 ELSE 0 END) AS losses
                FROM paper_positions WHERE status='CLOSED'
                """
            ).fetchone()
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        total = wins + losses
        return {
            "open": open_count,
            "open_by_phase": self.open_counts_by_phase(),
            "closed": closed_count,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100.0) if total else 0.0,
            "avg_return_pct": float(row["avg_return"] or 0.0),
        }

    def recent_positions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM paper_positions ORDER BY opened_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM paper_decisions ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]


class PaperTradingEngine:
    """Internal forward-test engine. It never submits broker orders."""

    def __init__(self, scanner_settings: Any, scanner_storage: Any) -> None:
        self.config = PaperTradingConfig.from_env(scanner_settings)
        self.scanner_settings = scanner_settings
        self.scanner_storage = scanner_storage
        self.store = PaperTradingStore(self.config.database_path)

    def process_scan(self, result: ScanResult, provider: Any) -> dict[str, Any]:
        if not self.config.enabled:
            return {"mode": self.config.mode, "opened": 0, "closed": 0, "open": 0}

        closed = self._monitor_open_positions(provider, result.timestamp)
        opened = self._consider_candidates(result, provider)
        summary = self.store.summary()
        summary.update({"mode": self.config.mode, "opened_now": opened, "closed_now": closed})
        return summary

    def _consider_candidates(self, result: ScanResult, provider: Any) -> int:
        opened = 0
        now = _utc(result.timestamp)
        phase = market_phase_at(now)
        profile = self.config.profile

        if profile in {"top_mover_pilot", "top_mover_10_shadow"}:
            # Zuerst bleibt das Forschungsuniversum auf die stÃ¤rksten Tagesmover
            # begrenzt. Innerhalb dieser Gruppe werden freie Portfolio-PlÃ¤tze
            # jedoch nach Kauf-Score vergeben, nicht nach bloÃŸem Tagesanstieg.
            mover_pool = sorted(
                result.all_observations or result.candidates,
                key=lambda candidate: (
                    self._mover_metrics(candidate)[0],
                    self._mover_metrics(candidate)[1],
                    float(candidate.signal_score),
                ),
                reverse=True,
            )[: self.config.top_mover_candidates_per_scan]
            candidates = sorted(
                mover_pool,
                key=lambda candidate: self._top_mover_priority_key(candidate, phase),
                reverse=True,
            )
            recommendations: dict[str, dict[str, Any]] = {}
        else:
            candidates = list(result.candidates)
            recommendations = self.scanner_storage.recommendations_for(
                [candidate.symbol for candidate in candidates]
            )

        for candidate in candidates:
            mover_change, mover_rvol = self._mover_metrics(candidate)

            if profile in {"top_mover_pilot", "top_mover_10_shadow"}:
                # Jeder abgeschlossene Scan ist eine neue, prospektive Beobachtung.
                # Der Cooldown verhindert trotzdem Mehrfachpositionen im selben Symbol.
                first_seen_at = candidate.timestamp or result.timestamp
                age = signal_age_minutes(first_seen_at, now)
                assessment = self._top_mover_assessment(candidate, phase)
                recommendation_key = str(result.run_id)
                trade_key = (
                    f"{profile}:{candidate.symbol.upper()}:{recommendation_key}"
                )
            else:
                rec = recommendations.get(candidate.symbol.upper(), {})
                first_seen_at = rec.get("first_seen_at") or candidate.timestamp
                age = signal_age_minutes(first_seen_at, now)
                standard = purchase_assessment(
                    candidate.signal_score,
                    candidate.risk_score,
                    candidate.model_probability,
                    candidate.features,
                    age_minutes=age,
                    phase=phase,
                )
                assessment = PilotAssessment(
                    score=float(standard.score),
                    quality=str(standard.quality),
                    eligible=bool(standard.eligible),
                    blockers=list(standard.blockers),
                    execution_quality=float(standard.execution_quality),
                    mover_change_pct=mover_change * 100.0,
                    mover_relative_volume=mover_rvol,
                )
                recommendation_key = str(first_seen_at or candidate.timestamp)
                trade_key = f"{candidate.symbol.upper()}:{recommendation_key}"

            priority_band = self._priority_band(assessment.score)
            reasons: list[str] = []

            # Qualit?ts-Test: In der bisherigen Shadow-Stichprobe waren
            # Top-Mover-Pilot-Entries mit Signal-Score > 60 schwach.
            # Nur f?r den Top-Mover-Pilot blockieren.
            if (
                profile == "top_mover_pilot"
                and float(candidate.signal_score) > 60.0
            ):
                reasons.append("Signal > 60 (Qualitaetsfilter)")

            if not assessment.eligible:
                reasons.extend(assessment.blockers or ["Kaufbewertung nicht freigegeben"])
            if assessment.score < self.config.min_purchase_score:
                reasons.append(f"Kauf-Score < {self.config.min_purchase_score:.1f}")
            if not (self.config.min_entry_price <= candidate.price <= self.config.max_entry_price):
                reasons.append(
                    f"Einstiegskurs auÃŸerhalb {self.config.min_entry_price:.2f}â€“"
                    f"{self.config.max_entry_price:.2f} USD"
                )
            if age > self.config.max_signal_age_minutes:
                reasons.append(f"Signal Ã¤lter als {self.config.max_signal_age_minutes:.0f} Minuten")
            open_count = len(self.store.open_positions())
            if open_count >= self.config.max_open_positions:
                reasons.append(
                    "globales Limit offener Positionen erreicht "
                    f"({open_count}/{self.config.max_open_positions})"
                )

            phase_limit = self.config.max_open_for_phase(phase)
            phase_open_count = self.store.open_count_for_phase(phase)
            if phase_limit <= 0:
                reasons.append(f"keine neuen Positionen in Marktphase {phase}")
            elif phase_open_count >= phase_limit:
                reasons.append(
                    f"Phasenlimit {phase} erreicht "
                    f"({phase_open_count}/{phase_limit})"
                )

            opened_today = self.store.opened_count_for_et_date(_et_date(now))
            if opened_today >= self.config.max_new_trades_per_day:
                reasons.append(
                    "Tageslimit neuer Papertrades erreicht "
                    f"({opened_today}/{self.config.max_new_trades_per_day})"
                )
            if self.store.has_trade_key(trade_key):
                reasons.append("Scan-Episode bereits verarbeitet")

            cooldown_start = now - timedelta(hours=self.config.cooldown_hours)
            if self.store.recently_traded(candidate.symbol, cooldown_start):
                reasons.append(f"{self.config.cooldown_hours:g}h-Cooldown aktiv")

            entry_guard: EntryGuardResult | None = None
            if not reasons and self.config.entry_guard_enabled:
                entry_guard = self._entry_guard(
                    candidate,
                    phase,
                    provider,
                    now=datetime.now(timezone.utc),
                )
                if not entry_guard.allowed:
                    reasons.append(
                        f"Entry-Guard {entry_guard.result}: {entry_guard.reason}"
                    )

            metadata = {
                "candidate": candidate.to_dict(),
                "assessment": asdict(assessment),
                "first_seen_at": first_seen_at,
                "age_minutes": age,
                "strategy_profile": profile,
                "purchase_priority": priority_band,
                "portfolio_limits": {
                    "global": self.config.max_open_positions,
                    "phase": phase_limit,
                    "daily": self.config.max_new_trades_per_day,
                },
                "mover_change_pct": mover_change * 100.0,
                "mover_relative_volume": mover_rvol,
                "entry_guard": asdict(entry_guard) if entry_guard else None,
            }
            if reasons:
                self.store.record_decision(
                    created_at=now.isoformat(),
                    run_id=result.run_id,
                    candidate=candidate,
                    mode=self.config.mode,
                    decision="REJECTED",
                    reason=" | ".join(dict.fromkeys(reasons)),
                    phase=phase,
                    purchase_score=assessment.score,
                    strategy_profile=profile,
                    mover_change_pct=mover_change * 100.0,
                    mover_relative_volume=mover_rvol,
                    entry_guard=entry_guard,
                    metadata=metadata,
                )
                continue

            if entry_guard is None:
                entry_guard = EntryGuardResult(
                    allowed=True,
                    result="DISABLED",
                    reason="Entry-Guard deaktiviert",
                    scan_reference_price=float(candidate.price),
                    refreshed_entry_price=float(candidate.price),
                    drift_pct=0.0,
                    quote_at=None,
                    quote_age_seconds=None,
                    spread_pct=max(
                        0.0,
                        float(candidate.features.get("spread_pct", 0.0) or 0.0),
                    ),
                    best_bid=None,
                    best_ask=None,
                    source="scan_candidate",
                    notes=[],
                )

            spread = max(0.0, float(entry_guard.spread_pct or 0.0))
            entry_reference = float(
                entry_guard.refreshed_entry_price
                if entry_guard.refreshed_entry_price is not None
                else candidate.price
            )
            entry_fill = self._buy_fill(entry_reference, spread)
            expires_at = now + timedelta(hours=self.config.max_hold_hours)
            self.store.create_position(
                {
                    "trade_key": trade_key,
                    "mode": self.config.mode,
                    "strategy_profile": profile,
                    "status": "OPEN",
                    "symbol": candidate.symbol.upper(),
                    "source_run_id": result.run_id,
                    "source_recommendation_at": recommendation_key,
                    "phase": phase,
                    "opened_at": now.isoformat(),
                    "opened_trade_date_et": _et_date(now),
                    "expires_at": expires_at.isoformat(),
                    "entry_reference_price": entry_reference,
                    "entry_fill_price": entry_fill,
                    "entry_spread_pct": spread,
                    "stop_price": entry_fill * (1.0 - self.config.stop_loss_pct),
                    "target_price": entry_fill * (1.0 + self.config.target_pct),
                    "signal_score": float(candidate.signal_score),
                    "risk_score": float(candidate.risk_score),
                    "purchase_score": float(assessment.score),
                    "purchase_quality": assessment.quality,
                    "model_probability": candidate.model_probability,
                    "mover_change_pct": mover_change * 100.0,
                    "mover_relative_volume": mover_rvol,
                    "scan_reference_price": float(candidate.price),
                    "refreshed_entry_price": entry_reference,
                    "entry_price_drift_pct": entry_guard.drift_pct,
                    "entry_quote_at": entry_guard.quote_at,
                    "entry_quote_age_seconds": entry_guard.quote_age_seconds,
                    "entry_guard_result": entry_guard.result,
                    "entry_guard_source": entry_guard.source,
                    "entry_guard_spread_pct": spread,
                    "last_bar_at": None,
                    "last_price": entry_reference,
                    "max_high": entry_reference,
                    "min_low": entry_reference,
                    "max_favorable_pct": 0.0,
                    "max_adverse_pct": 0.0,
                    "metadata_json": _safe_json(metadata),
                }
            )
            self.store.record_decision(
                created_at=now.isoformat(),
                run_id=result.run_id,
                candidate=candidate,
                mode=self.config.mode,
                decision="OPENED",
                reason=(
                    (
                        f"Top-Mover-10%-Shadow PrioritÃ¤t {priority_band}: "
                        f"Entry-Guard {entry_guard.result}; "
                        "simulierter Einstieg; keine Brokerorder"
                        if profile == "top_mover_10_shadow"
                        else (
                            f"Top-Mover-Pilot PrioritÃ¤t {priority_band}: "
                            f"Entry-Guard {entry_guard.result}; "
                            "simulierter Einstieg; keine Brokerorder"
                        )
                    )
                    if profile in {"top_mover_pilot", "top_mover_10_shadow"}
                    else (
                        f"Entry-Guard {entry_guard.result}; simulierter Einstieg; "
                        "keine Brokerorder"
                    )
                ),
                phase=phase,
                purchase_score=assessment.score,
                strategy_profile=profile,
                mover_change_pct=mover_change * 100.0,
                mover_relative_volume=mover_rvol,
                entry_guard=entry_guard,
                metadata=metadata,
            )
            opened += 1
            if (
                self.store.opened_count_for_et_date(_et_date(now))
                >= self.config.max_new_trades_per_day
            ):
                break
            if len(self.store.open_positions()) >= self.config.max_open_positions:
                break
            if (
                self.store.open_count_for_phase(phase)
                >= self.config.max_open_for_phase(phase)
            ):
                break
        return opened

    def _entry_guard(
        self,
        candidate: Candidate,
        phase: str,
        provider: Any,
        *,
        now: datetime | None = None,
    ) -> EntryGuardResult:
        checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        scan_price = float(candidate.price)
        notes: list[str] = []

        try:
            bars = provider.get_bars(
                candidate.symbol,
                self.config.entry_guard_bar_count,
            )
        except Exception as exc:
            return EntryGuardResult(
                allowed=False,
                result="REFRESH_ERROR",
                reason=f"frischer Minutenkurs nicht verfÃ¼gbar: {exc}",
                scan_reference_price=scan_price,
                refreshed_entry_price=None,
                drift_pct=None,
                quote_at=None,
                quote_age_seconds=None,
                spread_pct=None,
                best_bid=None,
                best_ask=None,
                source="none",
                notes=[],
            )

        if bars is None or bars.empty:
            return EntryGuardResult(
                allowed=False,
                result="REFRESH_ERROR",
                reason="frischer Minutenkurs ist leer",
                scan_reference_price=scan_price,
                refreshed_entry_price=None,
                drift_pct=None,
                quote_at=None,
                quote_age_seconds=None,
                spread_pct=None,
                best_bid=None,
                best_ask=None,
                source="none",
                notes=[],
            )

        frame = bars.copy()
        if "timestamp" not in frame or "close" not in frame:
            return EntryGuardResult(
                allowed=False,
                result="REFRESH_ERROR",
                reason="Minutenbalken enthalten Zeit oder Schlusskurs nicht",
                scan_reference_price=scan_price,
                refreshed_entry_price=None,
                drift_pct=None,
                quote_at=None,
                quote_age_seconds=None,
                spread_pct=None,
                best_bid=None,
                best_ask=None,
                source="none",
                notes=[],
            )

        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"],
            utc=True,
            errors="coerce",
        )
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna(subset=["timestamp", "close"])
        frame = frame[frame["close"] > 0].sort_values("timestamp")
        if frame.empty:
            return EntryGuardResult(
                allowed=False,
                result="REFRESH_ERROR",
                reason="kein gÃ¼ltiger Minutenkurs vorhanden",
                scan_reference_price=scan_price,
                refreshed_entry_price=None,
                drift_pct=None,
                quote_at=None,
                quote_age_seconds=None,
                spread_pct=None,
                best_bid=None,
                best_ask=None,
                source="none",
                notes=[],
            )

        latest = frame.iloc[-1]
        quote_at_dt = latest["timestamp"].to_pydatetime().astimezone(timezone.utc)
        quote_age = max(0.0, (checked_at - quote_at_dt).total_seconds())
        refreshed_price = float(latest["close"])
        source = "latest_bar_close"

        best_bid: float | None = None
        best_ask: float | None = None
        spread: float | None = None
        try:
            book = provider.get_order_book(candidate.symbol, depth=1)
            bids = book.get("bids", []) if isinstance(book, dict) else []
            asks = book.get("asks", []) if isinstance(book, dict) else []
            if bids and asks:
                bid_prices = [
                    float(row.get("price"))
                    for row in bids
                    if isinstance(row, dict) and row.get("price") is not None
                ]
                ask_prices = [
                    float(row.get("price"))
                    for row in asks
                    if isinstance(row, dict) and row.get("price") is not None
                ]
                best_bid = max(bid_prices) if bid_prices else None
                best_ask = min(ask_prices) if ask_prices else None
                if (
                    best_bid is not None
                    and best_ask is not None
                    and best_bid > 0
                    and best_ask > best_bid
                ):
                    midpoint = (best_bid + best_ask) / 2.0
                    spread = (best_ask - best_bid) / midpoint
                    refreshed_price = midpoint
                    source = "order_book_midpoint"
                else:
                    notes.append("Orderbuch ungÃ¼ltig; Minutenkurs verwendet")
                    best_bid = None
                    best_ask = None
            else:
                notes.append("Orderbuch unvollstÃ¤ndig; Minutenkurs verwendet")
        except Exception as exc:
            notes.append(f"Orderbuch nicht verfÃ¼gbar: {exc}")

        if spread is None:
            try:
                spread = max(
                    0.0,
                    float(candidate.features.get("spread_pct", 0.0) or 0.0),
                )
            except (TypeError, ValueError):
                spread = 0.0
            notes.append("Spread aus Scanmerkmal verwendet")

        drift = (
            refreshed_price / scan_price - 1.0
            if scan_price > 0
            else 0.0
        )

        result = "PASS"
        reason = "frischer Einstieg innerhalb aller Guard-Grenzen"
        allowed = True

        if quote_age > self.config.entry_guard_max_quote_age_seconds:
            result = "STALE"
            reason = (
                f"Kursinformation {quote_age:.0f}s alt; maximal "
                f"{self.config.entry_guard_max_quote_age_seconds:.0f}s"
            )
            allowed = False
        elif drift > self.config.entry_guard_max_chase_pct:
            result = "CHASE"
            reason = (
                f"Kurs seit Scan um {drift * 100.0:+.2f}% gestiegen; maximal "
                f"+{self.config.entry_guard_max_chase_pct * 100.0:.2f}%"
            )
            allowed = False
        elif drift < -self.config.entry_guard_max_breakdown_pct:
            result = "BREAKDOWN"
            reason = (
                f"Kurs seit Scan um {drift * 100.0:+.2f}% gefallen; maximal "
                f"-{self.config.entry_guard_max_breakdown_pct * 100.0:.2f}%"
            )
            allowed = False
        elif not (
            self.config.min_entry_price
            <= refreshed_price
            <= self.config.max_entry_price
        ):
            result = "PRICE"
            reason = (
                f"frischer Kurs auÃŸerhalb {self.config.min_entry_price:.2f}â€“"
                f"{self.config.max_entry_price:.2f} USD"
            )
            allowed = False
        else:
            phase_code = str(phase or "OFF").upper()
            spread_limit = (
                self.config.top_mover_max_spread_rth
                if phase_code == "RTH"
                else self.config.top_mover_max_spread_extended
            )
            if spread > spread_limit:
                result = "SPREAD"
                reason = (
                    f"frischer Spread {spread * 100.0:.2f}% Ã¼ber "
                    f"{spread_limit * 100.0:.2f}%"
                )
                allowed = False

        return EntryGuardResult(
            allowed=allowed,
            result=result,
            reason=reason,
            scan_reference_price=round(scan_price, 8),
            refreshed_entry_price=round(refreshed_price, 8),
            drift_pct=round(drift * 100.0, 4),
            quote_at=quote_at_dt.isoformat(),
            quote_age_seconds=round(quote_age, 1),
            spread_pct=round(float(spread), 8),
            best_bid=best_bid,
            best_ask=best_ask,
            source=source,
            notes=notes,
        )

    @staticmethod
    def _priority_band(score: float) -> str:
        value = float(score)
        if value >= 75.0:
            return "A"
        if value >= 65.0:
            return "B"
        return "C"

    def _top_mover_priority_key(
        self,
        candidate: Candidate,
        phase: str,
    ) -> tuple[float, float, float, float]:
        assessment = self._top_mover_assessment(candidate, phase)
        change, relative_volume = self._mover_metrics(candidate)
        return (
            float(assessment.score),
            float(change),
            float(relative_volume),
            float(candidate.signal_score),
        )

    @staticmethod
    def _mover_metrics(candidate: Candidate) -> tuple[float, float]:
        features = candidate.features or {}

        try:
            change = float(features.get("screener_change_ratio", 0.0) or 0.0)
        except (TypeError, ValueError):
            change = 0.0

        rvol_values: list[float] = []
        for key in ("screener_relative_volume", "relative_volume_5m"):
            try:
                rvol_values.append(float(features.get(key, 0.0) or 0.0))
            except (TypeError, ValueError):
                continue

        return max(0.0, change), max([0.0, *rvol_values])

    def _top_mover_assessment(
        self,
        candidate: Candidate,
        phase: str,
    ) -> PilotAssessment:
        features = candidate.features or {}
        change, relative_volume = self._mover_metrics(candidate)

        try:
            spread = max(0.0, float(features.get("spread_pct", 0.0) or 0.0))
        except (TypeError, ValueError):
            spread = 0.0
        try:
            dollar_volume = max(
                0.0, float(features.get("dollar_volume_5m", 0.0) or 0.0)
            )
        except (TypeError, ValueError):
            dollar_volume = 0.0

        signal = max(0.0, min(100.0, float(candidate.signal_score)))
        risk = max(0.0, min(100.0, float(candidate.risk_score)))
        execution = execution_quality_score(features)

        min_change = self.config.top_mover_min_change_pct
        min_rvol = self.config.top_mover_min_relative_volume
        change_span = max(0.01, 0.50 - min_change)
        rvol_span = max(0.5, 10.0 - min_rvol)

        change_score = max(
            0.0,
            min(100.0, 50.0 + (change - min_change) / change_span * 50.0),
        )
        rvol_score = max(
            0.0,
            min(100.0, 50.0 + (relative_volume - min_rvol) / rvol_span * 50.0),
        )

        score = (
            0.25 * signal
            + 0.20 * (100.0 - risk)
            + 0.25 * change_score
            + 0.15 * rvol_score
            + 0.15 * execution
        )

        phase = str(phase or "OFF").upper()
        blockers: list[str] = []
        if phase == "OFF":
            blockers.append("Marktphase OFF")
        if change < min_change:
            blockers.append(f"Top-Mover-Anstieg < {min_change:.0%}")
        if relative_volume < min_rvol:
            blockers.append(f"Top-Mover-RVOL < {min_rvol:g}")
        if signal < self.config.top_mover_min_signal_score:
            blockers.append(
                f"Signal < {self.config.top_mover_min_signal_score:.0f}"
            )
        if risk > self.config.top_mover_max_risk_score:
            blockers.append(
                f"Risiko > {self.config.top_mover_max_risk_score:.0f}"
            )
        spread_limit = (
            self.config.top_mover_max_spread_rth
            if phase == "RTH"
            else self.config.top_mover_max_spread_extended
        )
        if spread > spread_limit:
            blockers.append(f"Spread > {spread_limit:.0%}")
        if dollar_volume < self.config.top_mover_min_dollar_volume_5m:
            blockers.append(
                "5-Minuten-Dollarvolumen < "
                f"{self.config.top_mover_min_dollar_volume_5m:,.0f} USD"
            )

        eligible = not blockers and score >= self.config.min_purchase_score
        if blockers:
            quality = "TOP-MOVER AUSSCHLUSS"
        elif score >= 70.0:
            quality = "TOP-MOVER STARK"
        elif eligible:
            quality = "TOP-MOVER PILOT"
        else:
            quality = "TOP-MOVER BEOBACHTEN"

        return PilotAssessment(
            score=round(score, 2),
            quality=quality,
            eligible=eligible,
            blockers=blockers,
            execution_quality=round(execution, 2),
            mover_change_pct=round(change * 100.0, 2),
            mover_relative_volume=round(relative_volume, 2),
        )

    def _monitor_open_positions(self, provider: Any, scan_timestamp: str) -> int:
        closed = 0
        now = _utc(scan_timestamp)
        for position in self.store.open_positions():
            try:
                bars = provider.get_bars(position["symbol"], self.config.monitor_bar_count)
                closed += int(self._apply_bars(position, bars, now))
            except Exception as exc:
                logger.warning(
                    "Papertrading-Monitor %s konnte nicht aktualisiert werden: %s",
                    position["symbol"],
                    exc,
                )
        return closed

    def _apply_bars(
        self, position: dict[str, Any], bars: pd.DataFrame, now: datetime
    ) -> bool:
        if bars is None or bars.empty:
            return False
        frame = bars.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
        opened_at = _utc(position["opened_at"])
        last_bar_at = _utc(position["last_bar_at"]) if position.get("last_bar_at") else opened_at
        frame = frame[frame["timestamp"] > pd.Timestamp(last_bar_at)].sort_values("timestamp")
        if frame.empty:
            return False

        entry = float(position["entry_fill_price"])
        stop = float(position["stop_price"])
        target = float(position["target_price"])
        expires = _utc(position["expires_at"])
        spread = float(position["entry_spread_pct"] or 0.0)
        max_high = float(position["max_high"])
        min_low = float(position["min_low"])
        last_price = float(position["last_price"] or entry)
        last_time = last_bar_at

        for row in frame.itertuples(index=False):
            bar_time = row.timestamp.to_pydatetime().astimezone(timezone.utc)
            high = float(row.high)
            low = float(row.low)
            close = float(row.close)
            open_price = float(row.open)
            max_high = max(max_high, high)
            min_low = min(min_low, low)
            last_price = close
            last_time = bar_time

            # Conservatively assume the stop was hit first if both levels lie in one bar.
            hit_stop = low <= stop
            hit_target = high >= target
            if (
                hit_stop
                and hit_target
                and self.config.profile == "top_mover_10_shadow"
            ):
                # FÃ¼r den separaten Forschungs-Shadow wird kein Gewinner oder
                # Verlierer erfunden, wenn beide Marken erstmals im selben
                # Minutenbalken liegen.
                self._close_position(
                    position,
                    bar_time,
                    "AMBIGUOUS",
                    stop,
                    spread,
                    max_high,
                    min_low,
                )
                return True
            if hit_stop:
                reference = min(stop, open_price) if open_price < stop else stop
                self._close_position(
                    position, bar_time, "STOP", reference, spread, max_high, min_low
                )
                return True
            if hit_target:
                self._close_position(
                    position, bar_time, "TARGET", target, spread, max_high, min_low
                )
                return True
            if bar_time >= expires:
                self._close_position(
                    position, bar_time, "TIME", close, spread, max_high, min_low
                )
                return True

        self.store.update_position(
            int(position["id"]),
            last_bar_at=last_time.isoformat(),
            last_price=last_price,
            max_high=max_high,
            min_low=min_low,
            max_favorable_pct=(max_high / entry - 1.0) * 100.0,
            max_adverse_pct=(min_low / entry - 1.0) * 100.0,
        )
        if now >= expires and last_time >= expires:
            self._close_position(position, last_time, "TIME", last_price, spread, max_high, min_low)
            return True
        return False

    def _buy_fill(self, reference: float, spread_pct: float) -> float:
        ask = reference * (1.0 + max(0.0, spread_pct) / 2.0)
        return ask * (1.0 + max(0.0, self.config.slippage_pct))

    def _sell_fill(self, reference: float, spread_pct: float) -> float:
        bid = reference * (1.0 - max(0.0, spread_pct) / 2.0)
        return bid * (1.0 - max(0.0, self.config.slippage_pct))

    def _close_position(
        self,
        position: dict[str, Any],
        closed_at: datetime,
        reason: str,
        reference_price: float,
        spread_pct: float,
        max_high: float,
        min_low: float,
    ) -> None:
        entry = float(position["entry_fill_price"])
        exit_fill = self._sell_fill(float(reference_price), spread_pct)
        gross = (float(reference_price) / float(position["entry_reference_price"]) - 1.0) * 100.0
        net = (exit_fill / entry - 1.0) * 100.0
        self.store.update_position(
            int(position["id"]),
            status="CLOSED",
            closed_at=closed_at.astimezone(timezone.utc).isoformat(),
            exit_reason=reason,
            exit_reference_price=float(reference_price),
            exit_fill_price=exit_fill,
            gross_return_pct=gross,
            net_return_pct=net,
            last_bar_at=closed_at.astimezone(timezone.utc).isoformat(),
            last_price=float(reference_price),
            max_high=max_high,
            min_low=min_low,
            max_favorable_pct=(max_high / entry - 1.0) * 100.0,
            max_adverse_pct=(min_low / entry - 1.0) * 100.0,
        )

