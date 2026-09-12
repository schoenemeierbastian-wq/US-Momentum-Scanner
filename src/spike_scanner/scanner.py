from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pandas as pd

from spike_scanner.config import Settings
from spike_scanner.features import compute_features
from spike_scanner.learning import LearningEngine
from spike_scanner.model_runtime import ProbabilityModelBundle
from spike_scanner.models import Candidate, ScanResult
from spike_scanner.paper_trading import PaperTradingEngine
from spike_scanner.top_mover_10_shadow import TopMover10ShadowEngine
from spike_scanner.providers import MarketDataProvider, is_transient_network_error
from spike_scanner.scoring import (
    MIN_TOP_SIGNAL_SCORE,
    combined_score,
    enforce_monotonic_probabilities,
    is_top_eligible,
    ranking_score,
    score_features,
    market_phase_at,
    purchase_assessment,
    signal_age_minutes,
)
from spike_scanner.storage import Storage

logger = logging.getLogger(__name__)


class MomentumScanner:
    def __init__(
        self,
        settings: Settings,
        provider: MarketDataProvider,
        storage: Storage | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.storage = storage or Storage(settings.database_path)
        self.models = ProbabilityModelBundle(settings)
        self.learning = LearningEngine(settings, provider, self.storage)
        self.paper = PaperTradingEngine(settings, self.storage)
        self.top_mover_10_shadow = TopMover10ShadowEngine(settings, self.storage)

    def scan_once(self) -> ScanResult:
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        errors: list[str] = []
        observations: list[Candidate] = []
        captured_symbols: set[str] = set()
        consecutive_transient_network_errors = 0

        try:
            universe = self.provider.discover_universe(self.settings.universe_size)
        except Exception as exc:
            result = ScanResult(
                run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), self.provider.name, 0, [], [], [str(exc)], {}
            )
            self.storage.save_scan(result)
            if self.settings.learning_enabled:
                summary = self.learning.run(result=None)
                result.learning_summary = summary.to_dict()
            return result

        for item in universe:
            try:
                if item.last_price is not None and not (
                    self.settings.min_price <= item.last_price <= self.settings.max_price
                ):
                    continue

                bars = self.provider.get_bars(item.symbol, self.settings.bar_count)
                consecutive_transient_network_errors = 0
                self.storage.save_market_bars(item.symbol, bars)
                captured_symbols.add(item.symbol.upper())
                order_book = None
                if self.settings.use_order_book:
                    try:
                        order_book = self.provider.get_order_book(item.symbol, depth=1)
                    except Exception as exc:
                        logger.warning("Orderbuch %s nicht verfügbar: %s", item.symbol, exc)

                features = compute_features(bars, item, order_book)
                if not (self.settings.min_price <= features["price"] <= self.settings.max_price):
                    continue
                if features["dollar_volume_5m"] < self.settings.min_dollar_volume_5m:
                    continue

                score = score_features(features)
                raw_probabilities = self.models.predict_all(features)
                probabilities = enforce_monotonic_probabilities(
                    raw_probabilities, self.settings.learning_thresholds
                )
                probability = self.models.primary_probability(probabilities)
                observations.append(
                    Candidate(
                        symbol=item.symbol,
                        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        price=round(features["price"], 6),
                        signal_score=score.signal_score,
                        risk_score=score.risk_score,
                        model_probability=probability,
                        model_probabilities=probabilities,
                        features=features,
                        reasons=score.reasons,
                    )
                )
            except Exception as exc:
                if is_transient_network_error(exc):
                    consecutive_transient_network_errors += 1
                    if consecutive_transient_network_errors >= 3:
                        logger.error(
                            "Scan nach %s aufeinanderfolgenden Netzwerkfehlern abgebrochen.",
                            consecutive_transient_network_errors,
                        )
                        raise
                else:
                    consecutive_transient_network_errors = 0

                message = f"{item.symbol}: {exc}"
                logger.exception("Analyse fehlgeschlagen: %s", message)
                errors.append(message)

        # Schwache technische Signale duerfen auch bei einem jungen/ueberoptimistischen
        # ML-Modell keine Top-Empfehlung werden. Sie bleiben aber im Gesamtuniversum
        # und damit als wertvolle positive/negative Lernfaelle erhalten.
        observations.sort(
            key=lambda candidate: (
                is_top_eligible(candidate.signal_score),
                ranking_score(
                    candidate.signal_score,
                    candidate.risk_score,
                    candidate.model_probability,
                ),
                combined_score(candidate.signal_score, candidate.risk_score),
                candidate.signal_score,
                -candidate.risk_score,
            ),
            reverse=True,
        )
        for rank, candidate in enumerate(observations, start=1):
            candidate.rank = rank

        top = [
            candidate for candidate in observations
            if is_top_eligible(candidate.signal_score)
        ][: self.settings.top_n]

        # Die Empfehlung ist erst nach Abschluss des gesamten Rankings bekannt.
        # Deshalb wird der prospektive Startzeitpunkt nicht auf den Scanbeginn
        # zurückdatiert: alle Kandidaten erhalten die tatsächliche Abschlusszeit.
        completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for candidate in observations:
            candidate.timestamp = completed_at

        result = ScanResult(
            run_id=run_id,
            timestamp=completed_at,
            mode=self.provider.name,
            universe_count=len(universe),
            candidates=top,
            all_observations=observations,
            errors=errors,
        )
        self.storage.save_scan(result)

        if self.settings.learning_enabled:
            summary = self.learning.run(
                result=result, captured_symbols=captured_symbols
            )
            result.learning_summary = summary.to_dict()
            if summary.models_accepted:
                self.models.reload()

        try:
            paper_summary = self.paper.process_scan(result, self.provider)
            if paper_summary.get("mode") != "off":
                logger.info(
                    "Papertrading %s: %s offen, %s neu, %s geschlossen",
                    paper_summary.get("mode"),
                    paper_summary.get("open", 0),
                    paper_summary.get("opened_now", 0),
                    paper_summary.get("closed_now", 0),
                )
        except Exception:
            logger.exception("Papertrading-Zyklus fehlgeschlagen")

        try:
            shadow_summary = self.top_mover_10_shadow.process_scan(result, self.provider)
            if shadow_summary.get("mode") != "off":
                logger.info(
                    "10%%-Shadow: %s offen, %s neu, %s geschlossen",
                    shadow_summary.get("open", 0),
                    shadow_summary.get("opened_now", 0),
                    shadow_summary.get("closed_now", 0),
                )
        except Exception:
            logger.exception("10%%-Shadow-Zyklus fehlgeschlagen")

        if observations:
            self._write_latest_csv(top)
        else:
            logger.warning(
                "Leerer Scanlauf: bestehende Kandidaten-CSV bleibt erhalten."
            )
        return result

    def run_learning_cycle(self, force_training: bool = False) -> dict:
        summary = self.learning.run(result=None, force_training=force_training)
        if summary.models_accepted:
            self.models.reload()
        return summary.to_dict()

    def _write_latest_csv(self, candidates: list[Candidate]) -> None:
        rows = []
        recommendations = self.storage.recommendations_for(
            [candidate.symbol for candidate in candidates]
        )
        for candidate in candidates:
            recommendation = recommendations.get(candidate.symbol.upper(), {})
            first_seen = recommendation.get("first_seen_at") or candidate.timestamp
            phase = market_phase_at(first_seen)
            assessment = purchase_assessment(
                candidate.signal_score,
                candidate.risk_score,
                candidate.model_probability,
                candidate.features,
                age_minutes=signal_age_minutes(first_seen),
                phase=phase,
            )
            row = {
                "rank": candidate.rank,
                "symbol": candidate.symbol,
                "timestamp_utc": candidate.timestamp,
                "first_recommendation_utc": recommendation.get("first_seen_at"),
                "first_recommendation_price": recommendation.get("first_seen_price"),
                "last_seen_utc": recommendation.get("last_seen_at"),
                "recommendation_seen_count": recommendation.get("seen_count"),
                "price": candidate.price,
                "signal_score": candidate.signal_score,
                "risk_score": candidate.risk_score,
                "overall_score": combined_score(candidate.signal_score, candidate.risk_score),
                "ranking_score": ranking_score(
                    candidate.signal_score, candidate.risk_score, candidate.model_probability
                ),
                "purchase_score": assessment.score,
                "purchase_quality": assessment.quality,
                "purchase_eligible": assessment.eligible,
                "purchase_phase": assessment.phase,
                "purchase_freshness": assessment.freshness,
                "purchase_execution_quality": assessment.execution_quality,
                "purchase_blockers": " | ".join(assessment.blockers),
                "top_eligible": is_top_eligible(candidate.signal_score),
                "minimum_top_signal_score": MIN_TOP_SIGNAL_SCORE,
                "model_probability": candidate.model_probability,
                "reasons": " | ".join(candidate.reasons),
            }
            for threshold in self.settings.learning_thresholds:
                row[f"probability_ge_{int(round(threshold * 100))}pct"] = (
                    candidate.model_probabilities.get(f"{threshold:.6f}")
                )
            for name in (
                "ret_5m",
                "ret_15m",
                "relative_volume_5m",
                "vwap_distance",
                "breakout_20",
                "spread_pct",
                "book_imbalance",
                "dollar_volume_5m",
            ):
                row[name] = candidate.features.get(name)
            rows.append(row)
        pd.DataFrame(rows).to_csv(self.settings.latest_csv_path, index=False)
