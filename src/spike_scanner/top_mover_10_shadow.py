from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spike_scanner.models import Candidate, ScanResult
from spike_scanner.paper_trading import (
    PaperTradingConfig,
    PaperTradingEngine,
    PaperTradingStore,
)
from spike_scanner.scoring import market_phase_at

logger = logging.getLogger(__name__)


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip().replace(",", "."))


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


@dataclass(slots=True)
class TopMover10ShadowConfig:
    enabled: bool = True
    database_path: Path = Path("data/top_mover_10_shadow.sqlite")
    max_open_positions: int = 4
    max_new_trades_per_day: int = 8
    max_open_pre: int = 1
    max_open_rth: int = 4
    pre_max_rank: int = 2
    rth_max_rank: int = 2
    observe_ath: bool = True
    observe_nonselected: bool = True
    candidate_pool: int = 10
    min_entry_price: float = 0.20
    max_entry_price: float = 10.00
    min_purchase_score: float = 55.0
    max_signal_age_minutes: float = 15.0
    stop_loss_pct: float = 0.07
    target_pct: float = 0.10
    max_hold_hours: float = 24.0
    slippage_pct: float = 0.0025
    monitor_bar_count: int = 600
    cooldown_hours: float = 4.0
    min_change_pct: float = 0.10
    min_relative_volume: float = 2.0
    min_signal_score: float = 35.0
    max_risk_score: float = 75.0
    max_spread_rth: float = 0.04
    max_spread_pre: float = 0.03
    min_dollar_volume_5m: float = 50_000.0
    entry_guard_enabled: bool = True
    entry_guard_bar_count: int = 5
    entry_guard_max_quote_age_seconds: float = 120.0
    entry_guard_max_chase_pct: float = 0.03
    entry_guard_max_breakdown_pct: float = 0.04

    @classmethod
    def from_env(cls) -> "TopMover10ShadowConfig":
        return cls(
            enabled=_as_bool("TOP_MOVER_10_SHADOW_ENABLED", True),
            database_path=Path(
                os.getenv(
                    "TOP_MOVER_10_SHADOW_DATABASE_PATH",
                    "data/top_mover_10_shadow.sqlite",
                )
            ),
            max_open_positions=max(
                1, _as_int("TOP_MOVER_10_SHADOW_MAX_OPEN_POSITIONS", 4)
            ),
            max_new_trades_per_day=max(
                1, _as_int("TOP_MOVER_10_SHADOW_MAX_NEW_PER_DAY", 8)
            ),
            max_open_pre=max(0, _as_int("TOP_MOVER_10_SHADOW_PRE_MAX_OPEN", 1)),
            max_open_rth=max(0, _as_int("TOP_MOVER_10_SHADOW_RTH_MAX_OPEN", 4)),
            pre_max_rank=max(1, _as_int("TOP_MOVER_10_SHADOW_PRE_MAX_RANK", 2)),
            rth_max_rank=max(1, _as_int("TOP_MOVER_10_SHADOW_RTH_MAX_RANK", 2)),
            observe_ath=_as_bool("TOP_MOVER_10_SHADOW_ATH_OBSERVE", True),
            observe_nonselected=_as_bool(
                "TOP_MOVER_10_SHADOW_OBSERVE_NONSELECTED", True
            ),
            candidate_pool=max(2, _as_int("TOP_MOVER_10_SHADOW_CANDIDATE_POOL", 10)),
            min_entry_price=_as_float("TOP_MOVER_10_SHADOW_MIN_PRICE", 0.20),
            max_entry_price=_as_float("TOP_MOVER_10_SHADOW_MAX_PRICE", 10.00),
            min_purchase_score=_as_float(
                "TOP_MOVER_10_SHADOW_MIN_PURCHASE_SCORE", 55.0
            ),
            max_signal_age_minutes=_as_float(
                "TOP_MOVER_10_SHADOW_MAX_SIGNAL_AGE_MINUTES", 15.0
            ),
            stop_loss_pct=_as_float("TOP_MOVER_10_SHADOW_STOP_PCT", 0.07),
            target_pct=_as_float("TOP_MOVER_10_SHADOW_TARGET_PCT", 0.10),
            max_hold_hours=_as_float("TOP_MOVER_10_SHADOW_MAX_HOLD_HOURS", 24.0),
            slippage_pct=_as_float("TOP_MOVER_10_SHADOW_SLIPPAGE_PCT", 0.0025),
            monitor_bar_count=max(
                120, _as_int("TOP_MOVER_10_SHADOW_MONITOR_BAR_COUNT", 600)
            ),
            cooldown_hours=_as_float("TOP_MOVER_10_SHADOW_COOLDOWN_HOURS", 4.0),
            min_change_pct=_as_float("TOP_MOVER_10_SHADOW_MIN_CHANGE_PCT", 0.10),
            min_relative_volume=_as_float(
                "TOP_MOVER_10_SHADOW_MIN_RELATIVE_VOLUME", 2.0
            ),
            min_signal_score=_as_float(
                "TOP_MOVER_10_SHADOW_MIN_SIGNAL_SCORE", 35.0
            ),
            max_risk_score=_as_float("TOP_MOVER_10_SHADOW_MAX_RISK_SCORE", 75.0),
            max_spread_rth=_as_float("TOP_MOVER_10_SHADOW_MAX_SPREAD_RTH", 0.04),
            max_spread_pre=_as_float("TOP_MOVER_10_SHADOW_MAX_SPREAD_PRE", 0.03),
            min_dollar_volume_5m=_as_float(
                "TOP_MOVER_10_SHADOW_MIN_DOLLAR_VOLUME_5M", 50_000.0
            ),
            entry_guard_enabled=_as_bool(
                "TOP_MOVER_10_SHADOW_ENTRY_GUARD_ENABLED", True
            ),
            entry_guard_bar_count=max(
                2, _as_int("TOP_MOVER_10_SHADOW_ENTRY_GUARD_BAR_COUNT", 5)
            ),
            entry_guard_max_quote_age_seconds=max(
                15.0,
                _as_float(
                    "TOP_MOVER_10_SHADOW_ENTRY_GUARD_MAX_QUOTE_AGE_SECONDS",
                    120.0,
                ),
            ),
            entry_guard_max_chase_pct=max(
                0.0,
                _as_float("TOP_MOVER_10_SHADOW_ENTRY_GUARD_MAX_CHASE_PCT", 0.03),
            ),
            entry_guard_max_breakdown_pct=max(
                0.0,
                _as_float(
                    "TOP_MOVER_10_SHADOW_ENTRY_GUARD_MAX_BREAKDOWN_PCT", 0.04
                ),
            ),
        )


class TopMover10ShadowEngine:
    """Parallel 10-percent shadow portfolio.

    RTH opens only the highest-ranked movers, PRE is capped at one open test
    position, ATH is observation-only. No broker connection is used.
    """

    PROFILE = "top_mover_10_shadow"

    def __init__(self, scanner_settings: Any, scanner_storage: Any) -> None:
        self.shadow = TopMover10ShadowConfig.from_env()
        self.scanner_settings = scanner_settings
        self.scanner_storage = scanner_storage

        paper_config = PaperTradingConfig(
            mode="shadow",
            profile=self.PROFILE,
            database_path=self.shadow.database_path,
            min_entry_price=self.shadow.min_entry_price,
            max_entry_price=self.shadow.max_entry_price,
            max_open_positions=self.shadow.max_open_positions,
            max_new_trades_per_day=self.shadow.max_new_trades_per_day,
            max_open_positions_pre=self.shadow.max_open_pre,
            max_open_positions_rth=self.shadow.max_open_rth,
            max_open_positions_ath=0,
            min_purchase_score=self.shadow.min_purchase_score,
            max_signal_age_minutes=self.shadow.max_signal_age_minutes,
            stop_loss_pct=self.shadow.stop_loss_pct,
            target_pct=self.shadow.target_pct,
            max_hold_hours=self.shadow.max_hold_hours,
            slippage_pct=self.shadow.slippage_pct,
            monitor_bar_count=self.shadow.monitor_bar_count,
            cooldown_hours=self.shadow.cooldown_hours,
            top_mover_min_change_pct=self.shadow.min_change_pct,
            top_mover_min_relative_volume=self.shadow.min_relative_volume,
            top_mover_min_signal_score=self.shadow.min_signal_score,
            top_mover_max_risk_score=self.shadow.max_risk_score,
            top_mover_max_spread_rth=self.shadow.max_spread_rth,
            top_mover_max_spread_extended=self.shadow.max_spread_pre,
            top_mover_min_dollar_volume_5m=self.shadow.min_dollar_volume_5m,
            top_mover_candidates_per_scan=self.shadow.candidate_pool,
            entry_guard_enabled=self.shadow.entry_guard_enabled,
            entry_guard_bar_count=self.shadow.entry_guard_bar_count,
            entry_guard_max_quote_age_seconds=self.shadow.entry_guard_max_quote_age_seconds,
            entry_guard_max_chase_pct=self.shadow.entry_guard_max_chase_pct,
            entry_guard_max_breakdown_pct=self.shadow.entry_guard_max_breakdown_pct,
        )

        # PaperTradingEngine supports an explicit config internally; constructing
        # it this way keeps the user's existing PAPERTRADING_* profile untouched.
        self.engine = PaperTradingEngine.__new__(PaperTradingEngine)
        self.engine.config = paper_config
        self.engine.scanner_settings = scanner_settings
        self.engine.scanner_storage = scanner_storage
        self.engine.store = PaperTradingStore(paper_config.database_path)
        self.store = self.engine.store

    @property
    def enabled(self) -> bool:
        return bool(self.shadow.enabled)

    def process_scan(self, result: ScanResult, provider: Any) -> dict[str, Any]:
        if not self.enabled:
            return {
                "mode": "off",
                "profile": self.PROFILE,
                "opened_now": 0,
                "closed_now": 0,
            }

        now = _utc(result.timestamp)
        phase = market_phase_at(now)

        # Existing positions are monitored during PRE, RTH, ATH and OFF. This
        # prevents a target or stop reached outside the opening phase from being
        # lost.
        closed = self.engine._monitor_open_positions(provider, result.timestamp)
        ranked = self._rank_movers(result)
        opened = 0
        observed = 0

        if phase in {"PRE", "RTH"}:
            rank_limit = (
                self.shadow.pre_max_rank if phase == "PRE" else self.shadow.rth_max_rank
            )
            selected = [candidate for candidate in ranked if candidate.rank <= rank_limit]
            nonselected = [candidate for candidate in ranked if candidate.rank > rank_limit]

            if self.shadow.observe_nonselected:
                for candidate in nonselected:
                    self._record_observation(
                        result,
                        candidate,
                        phase,
                        reason=(
                            f"{phase}-Beobachtung: Top-Mover-Rang {candidate.rank} "
                            f"liegt außerhalb des aktiven Rangs 1–{rank_limit}"
                        ),
                        role=f"{phase}_NONSELECTED",
                    )
                    observed += 1

            filtered = ScanResult(
                run_id=result.run_id,
                timestamp=result.timestamp,
                mode=result.mode,
                universe_count=result.universe_count,
                candidates=selected,
                all_observations=selected,
                errors=list(result.errors),
                learning_summary=dict(result.learning_summary),
            )
            opened = self.engine._consider_candidates(filtered, provider)

        elif phase == "ATH" and self.shadow.observe_ath:
            for candidate in ranked:
                self._record_observation(
                    result,
                    candidate,
                    phase,
                    reason="ATH-Beobachtung: keine 10%-Schattenposition im Postmarket",
                    role="ATH_OBSERVE",
                )
                observed += 1

        summary = self.store.summary()
        summary.update(
            {
                "mode": "shadow",
                "profile": self.PROFILE,
                "phase": phase,
                "opened_now": opened,
                "closed_now": closed,
                "observed_now": observed,
                "database_path": str(self.shadow.database_path),
                "rth_active_ranks": f"1-{self.shadow.rth_max_rank}",
                "pre_active_ranks": f"1-{self.shadow.pre_max_rank}",
                "ath_mode": "observe" if self.shadow.observe_ath else "off",
            }
        )
        return summary

    def _rank_movers(self, result: ScanResult) -> list[Candidate]:
        source = list(result.all_observations or result.candidates)
        ordered = sorted(
            source,
            key=lambda candidate: (
                self.engine._mover_metrics(candidate)[0],
                self.engine._mover_metrics(candidate)[1],
                float(candidate.signal_score),
            ),
            reverse=True,
        )[: self.shadow.candidate_pool]
        return [replace(candidate, rank=index) for index, candidate in enumerate(ordered, 1)]

    def _record_observation(
        self,
        result: ScanResult,
        candidate: Candidate,
        phase: str,
        *,
        reason: str,
        role: str,
    ) -> None:
        assessment = self.engine._top_mover_assessment(candidate, phase)
        mover_change, mover_rvol = self.engine._mover_metrics(candidate)
        metadata = {
            "candidate": candidate.to_dict(),
            "assessment": asdict(assessment),
            "strategy_profile": self.PROFILE,
            "shadow10_role": role,
            "top_mover_rank": int(candidate.rank),
            "target_pct": self.shadow.target_pct,
            "stop_loss_pct": self.shadow.stop_loss_pct,
            "no_broker_orders": True,
            "phase_policy": {
                "PRE": "active_test_max_1",
                "RTH": "active_core",
                "ATH": "observe_only",
                "OFF": "monitor_only",
            },
        }
        self.store.record_decision(
            created_at=_utc(result.timestamp).isoformat(),
            run_id=result.run_id,
            candidate=candidate,
            mode="shadow",
            decision="OBSERVED",
            reason=reason,
            phase=phase,
            purchase_score=assessment.score,
            strategy_profile=self.PROFILE,
            mover_change_pct=mover_change * 100.0,
            mover_relative_volume=mover_rvol,
            entry_guard=None,
            metadata=metadata,
        )
