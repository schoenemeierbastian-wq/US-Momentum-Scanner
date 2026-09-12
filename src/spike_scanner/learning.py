from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_score,
    recall_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from spike_scanner.config import Settings
from spike_scanner.model_runtime import model_path_for
from spike_scanner.models import ScanResult
from spike_scanner.providers.base import MarketDataProvider
from spike_scanner.storage import Storage

logger = logging.getLogger(__name__)


DEFAULT_FEATURES = [
    "ret_1m",
    "ret_5m",
    "ret_15m",
    "ret_30m",
    "momentum_acceleration",
    "relative_volume_5m",
    "volume_z_20",
    "vwap_distance",
    "breakout_20",
    "range_position",
    "realized_volatility_20",
    "dollar_volume_5m",
    "spread_pct",
    "book_imbalance",
    "screener_change_ratio",
    "screener_relative_volume",
]


@dataclass(slots=True)
class LearningCycleSummary:
    registered: int = 0
    bars_stored: int = 0
    symbols_refreshed: int = 0
    finalized: int = 0
    limited_finalized: int = 0
    due_checked: int = 0
    training_attempts: int = 0
    models_accepted: int = 0
    models_rejected: int = 0
    warnings: list[str] = field(default_factory=list)
    model_metrics: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registered": self.registered,
            "bars_stored": self.bars_stored,
            "symbols_refreshed": self.symbols_refreshed,
            "finalized": self.finalized,
            "limited_finalized": self.limited_finalized,
            "due_checked": self.due_checked,
            "training_attempts": self.training_attempts,
            "models_accepted": self.models_accepted,
            "models_rejected": self.models_rejected,
            "warnings": self.warnings,
            "model_metrics": self.model_metrics,
        }


def _parse_features(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def build_training_frame(
    storage: Storage,
    horizon_hours: float = 24,
    threshold_return: float = 0.20,
) -> pd.DataFrame:
    """Point-in-time frame from completed, prospectively registered events."""
    frame = storage.learning_events_frame(
        status="finalized", horizon_hours=horizon_hours
    )
    if frame.empty:
        return pd.DataFrame()
    if "data_quality" in frame.columns:
        frame = frame[frame["data_quality"] == "usable"]
    rows: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        features = _parse_features(row.get("features_json", "{}"))
        max_return = float(row.get("max_return") or 0.0)
        record = {
            name: float(features.get(name, 0.0) or 0.0)
            for name in DEFAULT_FEATURES
        }
        record.update(
            {
                "event_id": int(row["id"]),
                "symbol": str(row["symbol"]),
                "timestamp": pd.to_datetime(row["signal_timestamp"], utc=True),
                "future_max_return": max_return,
                "future_close_return": (
                    float(row.get("final_price") or row["entry_price"])
                    / float(row["entry_price"])
                    - 1.0
                ),
                "future_max_drawdown": float(row.get("max_drawdown") or 0.0),
                "bars_found": int(row.get("bars_found") or 0),
                "source_rank": int(row.get("source_rank") or 0),
                "label": int(max_return >= float(threshold_return)),
            }
        )
        rows.append(record)
    data = pd.DataFrame(rows)
    if not data.empty:
        data = data.sort_values("timestamp").reset_index(drop=True)
    return data


def _temporal_split(
    data: pd.DataFrame, horizon_hours: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological holdout with a purge gap to reduce overlapping outcomes."""
    for fraction in (0.80, 0.75, 0.70, 0.85, 0.65):
        split = max(1, min(len(data) - 1, int(len(data) * fraction)))
        test = data.iloc[split:].copy()
        if test.empty:
            continue
        test_start = pd.Timestamp(test["timestamp"].iloc[0])
        purge_before = test_start - pd.Timedelta(hours=float(horizon_hours))
        train = data[data["timestamp"] < purge_before].copy()
        if len(train) < 30:
            continue
        if train["label"].nunique() == 2 and test["label"].nunique() == 2:
            return train, test
    raise ValueError(
        "Der zeitliche Testabschnitt enthält noch nicht genügend unabhängige "
        "Gewinner und Nicht-Gewinner."
    )


def _candidate_models() -> dict[str, Any]:
    return {
        "Logistische Regression": Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        max_iter=4000,
                        class_weight="balanced",
                        C=0.35,
                        random_state=42,
                    ),
                ),
            ]
        ),
        "Kalibriertes Gradient Boosting": CalibratedClassifierCV(
            Pipeline(
                [
                    ("impute", SimpleImputer(strategy="median")),
                    (
                        "model",
                        HistGradientBoostingClassifier(
                            max_iter=220,
                            learning_rate=0.05,
                            max_leaf_nodes=15,
                            min_samples_leaf=20,
                            l2_regularization=2.0,
                            class_weight="balanced",
                            random_state=42,
                        ),
                    ),
                ]
            ),
            method="sigmoid",
            cv=3,
        ),
    }


def _daily_precision_at_k(test: pd.DataFrame, probabilities: np.ndarray, k: int) -> float:
    scored = test[["timestamp", "label"]].copy()
    scored["probability"] = probabilities
    scored["day"] = (
        pd.to_datetime(scored["timestamp"], utc=True)
        .dt.tz_convert("America/New_York")
        .dt.date
    )
    values: list[float] = []
    for _, group in scored.groupby("day"):
        chosen = group.nlargest(min(k, len(group)), "probability")
        if not chosen.empty:
            values.append(float(chosen["label"].mean()))
    return float(np.mean(values)) if values else 0.0


def _evaluate_model(
    model: Any, test: pd.DataFrame, feature_names: list[str]
) -> dict[str, float]:
    probabilities = np.asarray(model.predict_proba(test[feature_names])[:, 1], dtype=float)
    probabilities = np.clip(probabilities, 0.0, 1.0)
    predictions = (probabilities >= 0.5).astype(int)
    base_rate = float(test["label"].mean())
    decile_count = max(1, int(np.ceil(len(test) * 0.10)))
    top_decile = test.assign(probability=probabilities).nlargest(
        decile_count, "probability"
    )
    top_decile_precision = float(top_decile["label"].mean())
    return {
        "average_precision": float(
            average_precision_score(test["label"], probabilities)
        ),
        "brier_score": float(brier_score_loss(test["label"], probabilities)),
        "precision_at_0_5": float(
            precision_score(test["label"], predictions, zero_division=0)
        ),
        "recall_at_0_5": float(
            recall_score(test["label"], predictions, zero_division=0)
        ),
        "base_rate_test": base_rate,
        "top_decile_precision": top_decile_precision,
        "lift_at_top_decile": (
            float(top_decile_precision / base_rate) if base_rate > 0 else 0.0
        ),
        "daily_precision_at_3": _daily_precision_at_k(test, probabilities, 3),
        "daily_precision_at_10": _daily_precision_at_k(test, probabilities, 10),
    }


def _fit_best_challenger(
    train: pd.DataFrame, test: pd.DataFrame
) -> tuple[str, Any, dict[str, float]]:
    evaluated: list[tuple[str, Any, dict[str, float]]] = []
    for name, model in _candidate_models().items():
        try:
            model.fit(train[DEFAULT_FEATURES], train["label"])
            metrics = _evaluate_model(model, test, DEFAULT_FEATURES)
            evaluated.append((name, model, metrics))
        except Exception as exc:
            logger.warning("Modell %s konnte nicht trainiert werden: %s", name, exc)
    if not evaluated:
        raise ValueError("Keines der Kandidatenmodelle konnte trainiert werden.")
    evaluated.sort(
        key=lambda item: (
            item[2]["average_precision"],
            -item[2]["brier_score"],
            item[2]["daily_precision_at_3"],
        ),
        reverse=True,
    )
    return evaluated[0]


def train_and_compare_model(
    storage: Storage,
    settings: Settings,
    threshold_return: float,
) -> dict[str, Any]:
    horizon_hours = settings.learning_horizon_hours
    data = build_training_frame(storage, horizon_hours, threshold_return)
    if len(data) < settings.learning_min_rows:
        raise ValueError(
            f"Mindestens {settings.learning_min_rows} abgeschlossene Beobachtungen werden benötigt."
        )
    positives = int(data["label"].sum())
    negatives = int(len(data) - positives)
    if (
        positives < settings.learning_min_positives
        or negatives < settings.learning_min_positives
    ):
        raise ValueError(
            f"Benötigt werden mindestens {settings.learning_min_positives} Gewinner und "
            f"{settings.learning_min_positives} Nicht-Gewinner."
        )

    train, test = _temporal_split(data, horizon_hours)
    model_name, challenger, challenger_metrics = _fit_best_challenger(train, test)

    path = model_path_for(settings.model_path, horizon_hours, threshold_return)
    incumbent_metrics: dict[str, float] | None = None
    if path.exists():
        try:
            incumbent_payload = joblib.load(path)
            incumbent = incumbent_payload["model"]
            incumbent_features = list(
                incumbent_payload.get("feature_names") or DEFAULT_FEATURES
            )
            incumbent_metrics = _evaluate_model(
                incumbent, test, incumbent_features
            )
        except Exception as exc:
            logger.warning("Bisheriges Modell konnte nicht verglichen werden: %s", exc)

    base_rate = float(test["label"].mean())
    required_ap = base_rate * settings.learning_min_lift_over_base
    quality_gate = (
        challenger_metrics["average_precision"] >= required_ap
        and challenger_metrics["lift_at_top_decile"] >= 1.0
    )

    if incumbent_metrics is None:
        accepted = quality_gate
        reason = (
            "Erstes Modell erfüllt den Mindest-Lift gegenüber der Basisrate."
            if accepted
            else (
                "Erstes Modell noch nicht stark genug: "
                f"AP {challenger_metrics['average_precision']:.4f}, "
                f"erforderlich mindestens {required_ap:.4f}."
            )
        )
    else:
        ap_gain = (
            challenger_metrics["average_precision"]
            - incumbent_metrics["average_precision"]
        )
        brier_change = (
            challenger_metrics["brier_score"]
            - incumbent_metrics["brier_score"]
        )
        brier_improvement = -brier_change
        accepted = quality_gate and (
            (
                ap_gain >= settings.learning_min_ap_improvement
                and brier_change <= settings.learning_max_brier_degradation
            )
            or (ap_gain >= -0.002 and brier_improvement >= 0.01)
        )
        if accepted:
            reason = (
                f"Herausforderer übernommen: AP-Änderung {ap_gain:+.4f}, "
                f"Brier-Änderung {brier_change:+.4f}."
            )
        else:
            reason = (
                f"Bisheriges Modell bleibt: AP-Änderung {ap_gain:+.4f}, "
                f"Brier-Änderung {brier_change:+.4f}."
            )

    trained_at = datetime.now(timezone.utc).isoformat()
    metrics: dict[str, Any] = {
        "trained_at": trained_at,
        "model_name": model_name,
        "rows": int(len(data)),
        "positives": positives,
        "negatives": negatives,
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        **challenger_metrics,
        "incumbent_average_precision": (
            incumbent_metrics.get("average_precision") if incumbent_metrics else None
        ),
        "incumbent_brier_score": (
            incumbent_metrics.get("brier_score") if incumbent_metrics else None
        ),
        "horizon_hours": horizon_hours,
        "threshold_return": float(threshold_return),
        "positive_rate": float(positives / len(data)),
        "accepted": accepted,
        "reason": reason,
        "model_path": str(path),
    }

    if accepted:
        production_model = clone(challenger)
        production_model.fit(data[DEFAULT_FEATURES], data["label"])
        payload = {
            "model": production_model,
            "feature_names": DEFAULT_FEATURES,
            "metrics": metrics,
            "horizon_hours": horizon_hours,
            "threshold_return": float(threshold_return),
            "trained_at": trained_at,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        joblib.dump(payload, temporary)
        os.replace(temporary, path)
        if abs(threshold_return - settings.learning_primary_threshold) < 1e-9:
            joblib.dump(payload, settings.model_path)

    storage.record_model_run(metrics)
    return metrics


def train_probability_model(
    storage: Storage,
    model_path: Path,
    horizon_hours: float = 24,
    threshold_return: float = 0.20,
) -> dict[str, Any]:
    """Compatibility wrapper used by the CLI's manual training command."""
    settings = Settings(
        database_path=storage.path,
        model_path=model_path,
        learning_horizon_hours=horizon_hours,
        learning_thresholds=(threshold_return,),
        learning_primary_threshold=threshold_return,
        learning_min_ap_improvement=-1.0,
        learning_min_lift_over_base=1.0,
    )
    return train_and_compare_model(storage, settings, threshold_return)


class LearningEngine:
    def __init__(
        self,
        settings: Settings,
        provider: MarketDataProvider,
        storage: Storage,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.storage = storage

    def run(
        self,
        result: ScanResult | None = None,
        force_training: bool = False,
        captured_symbols: set[str] | None = None,
    ) -> LearningCycleSummary:
        summary = LearningCycleSummary()
        if not self.settings.learning_enabled and not force_training:
            return summary

        if result is not None:
            summary.registered = self.storage.register_learning_events(
                result=result,
                horizon_hours=self.settings.learning_horizon_hours,
                track_top_n=self.settings.learning_track_top_n,
                gap_minutes=self.settings.learning_event_gap_minutes,
            )

        self._refresh_pending_symbols(summary, captured_symbols or set())
        self._finalize_due_events(summary)
        self._maybe_retrain(summary, force=force_training)
        return summary

    def _refresh_pending_symbols(
        self, summary: LearningCycleSummary, captured_symbols: set[str]
    ) -> None:
        symbols = self.storage.active_learning_symbols(
            limit=self.settings.learning_refresh_symbols_per_scan
        )
        count = max(
            self.settings.bar_count,
            int(self.settings.learning_horizon_hours * 60 * 1.6) + 300,
        )
        for symbol in symbols:
            if symbol.upper() in {item.upper() for item in captured_symbols}:
                continue
            try:
                bars = self.provider.get_bars(symbol, count)
                stored = self.storage.save_market_bars(symbol, bars)
                summary.bars_stored += stored
                summary.symbols_refreshed += 1
            except Exception as exc:
                summary.warnings.append(
                    f"{symbol}: Nachverfolgung vorübergehend nicht möglich ({exc})"
                )

    def _finalize_due_events(self, summary: LearningCycleSummary) -> None:
        due = self.storage.due_learning_events(
            limit=self.settings.learning_finalize_per_scan,
            horizon_hours=self.settings.learning_horizon_hours,
        )
        summary.due_checked = len(due)
        history_count = max(
            self.settings.bar_count,
            int(self.settings.learning_horizon_hours * 60 * 1.6) + 300,
        )
        for event in due:
            event_id = int(event["id"])
            try:
                window = self.storage.market_bars_frame(
                    str(event["symbol"]),
                    str(event["signal_timestamp"]),
                    str(event["due_at"]),
                )
                if len(window) < self.settings.learning_min_bars_per_event:
                    try:
                        bars = self.provider.get_bars(str(event["symbol"]), history_count)
                        self.storage.save_market_bars(str(event["symbol"]), bars)
                        window = self.storage.market_bars_frame(
                            str(event["symbol"]),
                            str(event["signal_timestamp"]),
                            str(event["due_at"]),
                        )
                    except Exception:
                        pass

                if window.empty:
                    raise ValueError("Keine Kursdaten im 24-Stunden-Auswertungsfenster")

                entry = float(event["entry_price"])
                max_high = float(pd.to_numeric(window["high"], errors="coerce").max())
                min_low = float(pd.to_numeric(window["low"], errors="coerce").min())
                final_price = float(
                    pd.to_numeric(window["close"], errors="coerce").iloc[-1]
                )
                if not all(
                    np.isfinite(value) and value > 0
                    for value in (entry, max_high, min_low, final_price)
                ):
                    raise ValueError("Ungültige Kurswerte im Auswertungsfenster")

                bars_found = int(len(window))
                attempts = int(event.get("attempt_count") or 0)
                if (
                    bars_found < self.settings.learning_min_bars_per_event
                    and attempts < 5
                ):
                    raise ValueError(
                        f"Erst {bars_found} Kursbalken verfügbar; später erneut prüfen"
                    )
                quality = (
                    "usable"
                    if bars_found >= self.settings.learning_min_bars_per_event
                    else "limited"
                )
                self.storage.finalize_learning_event(
                    event_id=event_id,
                    max_high=max_high,
                    min_low=min_low,
                    final_price=final_price,
                    max_return=max_high / entry - 1.0,
                    max_drawdown=min_low / entry - 1.0,
                    bars_found=bars_found,
                    thresholds=self.settings.learning_thresholds,
                    data_quality=quality,
                )
                summary.finalized += 1
                if quality != "usable":
                    summary.limited_finalized += 1
            except Exception as exc:
                message = f"{event['symbol']}: Ergebnis noch nicht auswertbar ({exc})"
                self.storage.mark_learning_attempt(event_id, message)
                summary.warnings.append(message)

    def _maybe_retrain(
        self,
        summary: LearningCycleSummary,
        force: bool,
    ) -> None:
        now = datetime.now(timezone.utc)
        for threshold in self.settings.learning_thresholds:
            last_run = self.storage.latest_model_run(
                self.settings.learning_horizon_hours,
                threshold,
                accepted_only=False,
            )
            if not force and last_run:
                last_time = datetime.fromisoformat(
                    str(last_run["trained_at"]).replace("Z", "+00:00")
                )
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                if now - last_time < timedelta(
                    hours=self.settings.learning_retrain_hours
                ):
                    continue
                new_labels = self.storage.finalized_since(
                    str(last_run["trained_at"]),
                    horizon_hours=self.settings.learning_horizon_hours,
                )
                if new_labels < self.settings.learning_min_new_labels:
                    continue

            data = build_training_frame(
                self.storage,
                self.settings.learning_horizon_hours,
                threshold,
            )
            if len(data) < self.settings.learning_min_rows:
                continue
            positives = int(data["label"].sum())
            negatives = int(len(data) - positives)
            if (
                positives < self.settings.learning_min_positives
                or negatives < self.settings.learning_min_positives
            ):
                continue

            summary.training_attempts += 1
            try:
                metrics = train_and_compare_model(
                    self.storage,
                    self.settings,
                    threshold,
                )
                summary.model_metrics.append(metrics)
                if metrics.get("accepted"):
                    summary.models_accepted += 1
                else:
                    summary.models_rejected += 1
            except Exception as exc:
                summary.warnings.append(
                    f"Modellziel +{threshold:.0%}: Training nicht möglich ({exc})"
                )
