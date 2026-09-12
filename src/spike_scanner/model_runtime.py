from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from spike_scanner.config import Settings


def threshold_key(threshold: float) -> str:
    return f"{float(threshold):.6f}"


def threshold_slug(threshold: float) -> str:
    percent = int(round(float(threshold) * 100))
    return f"t{percent:03d}"


def horizon_slug(hours: float) -> str:
    rounded = int(round(float(hours) * 10))
    if rounded % 10 == 0:
        return f"h{rounded // 10}"
    return f"h{rounded / 10:g}".replace(".", "p")


def model_path_for(base_path: Path, horizon_hours: float, threshold: float) -> Path:
    return base_path.parent / (
        f"{base_path.stem}_{horizon_slug(horizon_hours)}_{threshold_slug(threshold)}"
        f"{base_path.suffix or '.joblib'}"
    )


class ProbabilityModel:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.payload: dict[str, Any] | None = None
        self.reload()

    def reload(self) -> None:
        self.payload = None
        if self.path.exists():
            try:
                self.payload = joblib.load(self.path)
            except Exception:
                self.payload = None

    @property
    def available(self) -> bool:
        return self.payload is not None

    @property
    def metrics(self) -> dict[str, Any]:
        if not self.payload:
            return {}
        return dict(self.payload.get("metrics") or {})

    def predict(self, features: dict[str, float]) -> float | None:
        if not self.payload:
            return None
        feature_names = self.payload["feature_names"]
        model = self.payload["model"]
        row = pd.DataFrame([{name: features.get(name, 0.0) for name in feature_names}])
        probability = float(model.predict_proba(row)[0, 1])
        return round(probability, 6)


class ProbabilityModelBundle:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.models: dict[str, ProbabilityModel] = {}
        self.reload()

    def reload(self) -> None:
        self.models = {}
        for threshold in self.settings.learning_thresholds:
            path = model_path_for(
                self.settings.model_path,
                self.settings.learning_horizon_hours,
                threshold,
            )
            model = ProbabilityModel(path)
            # Backward compatibility: the old single model path is treated as the
            # primary target only if no target-specific model exists.
            if (
                not model.available
                and abs(threshold - self.settings.learning_primary_threshold) < 1e-9
                and self.settings.model_path.exists()
            ):
                model = ProbabilityModel(self.settings.model_path)
            self.models[threshold_key(threshold)] = model

    def predict_all(self, features: dict[str, float]) -> dict[str, float]:
        predictions: dict[str, float] = {}
        for key, model in self.models.items():
            probability = model.predict(features)
            if probability is not None:
                predictions[key] = probability
        return predictions

    def primary_probability(self, predictions: dict[str, float]) -> float | None:
        return predictions.get(threshold_key(self.settings.learning_primary_threshold))

    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for threshold in self.settings.learning_thresholds:
            key = threshold_key(threshold)
            model = self.models[key]
            rows.append(
                {
                    "threshold": threshold,
                    "path": str(model.path),
                    "available": model.available,
                    "metrics": model.metrics,
                }
            )
        return rows
