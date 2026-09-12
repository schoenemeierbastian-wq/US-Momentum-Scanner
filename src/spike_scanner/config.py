from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _as_thresholds(value: str | None) -> tuple[float, ...]:
    if not value:
        return (0.10, 0.20, 0.50, 1.00)
    parsed: list[float] = []
    for item in value.replace(";", ",").split(","):
        item = item.strip().replace("%", "")
        if not item:
            continue
        number = float(item.replace(",", "."))
        if number > 1:
            number /= 100.0
        if 0 < number < 5:
            parsed.append(round(number, 6))
    if not parsed:
        return (0.10, 0.20, 0.50, 1.00)
    return tuple(sorted(set(parsed)))


@dataclass(slots=True)
class Settings:
    mode: str = "mock"
    webull_app_key: str = ""
    webull_app_secret: str = ""
    webull_region: str = "us"
    webull_api_endpoint: str = "api.webull.com"
    webull_token_dir: str = ".webull_tokens"

    universe_size: int = 25
    top_n: int = 3
    bar_count: int = 120
    scan_interval_seconds: int = 300
    use_order_book: bool = True
    request_delay_seconds: float = 1.05
    network_retry_attempts: int = 4
    network_retry_base_seconds: float = 2.0
    network_error_retry_seconds: int = 60
    min_price: float = 0.20
    max_price: float = 50.0
    min_dollar_volume_5m: float = 25_000.0

    database_path: Path = Path("data/scanner.db")
    latest_csv_path: Path = Path("output/latest_candidates.csv")
    model_path: Path = Path("models/momentum_model.joblib")

    # Automatischer prospektiver Lernzyklus.
    learning_enabled: bool = True
    learning_horizon_hours: float = 24.0
    learning_thresholds: tuple[float, ...] = (0.10, 0.20, 0.50, 1.00)
    learning_primary_threshold: float = 0.20
    learning_track_top_n: int = 50
    learning_event_gap_minutes: int = 120
    learning_finalize_per_scan: int = 30
    learning_refresh_symbols_per_scan: int = 30
    learning_min_bars_per_event: int = 8
    learning_retrain_hours: int = 24
    learning_min_rows: int = 200
    learning_min_positives: int = 20
    learning_min_new_labels: int = 40
    learning_min_ap_improvement: float = 0.005
    learning_max_brier_degradation: float = 0.02
    learning_min_lift_over_base: float = 1.10

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Settings":
        load_dotenv(dotenv_path=env_file, override=False)
        thresholds = _as_thresholds(os.getenv("LEARNING_THRESHOLDS"))
        primary = float(os.getenv("LEARNING_PRIMARY_THRESHOLD", "0.20"))
        if primary > 1:
            primary /= 100.0
        if primary not in thresholds:
            thresholds = tuple(sorted(set((*thresholds, round(primary, 6)))))
        return cls(
            mode=os.getenv("SCANNER_MODE", "mock").lower(),
            webull_app_key=os.getenv("WEBULL_APP_KEY", ""),
            webull_app_secret=os.getenv("WEBULL_APP_SECRET", ""),
            webull_region=os.getenv("WEBULL_REGION", "us"),
            webull_api_endpoint=os.getenv("WEBULL_API_ENDPOINT", "api.webull.com"),
            webull_token_dir=os.getenv("WEBULL_TOKEN_DIR", ".webull_tokens"),
            universe_size=int(os.getenv("UNIVERSE_SIZE", "25")),
            top_n=int(os.getenv("TOP_N", "3")),
            bar_count=int(os.getenv("BAR_COUNT", "120")),
            scan_interval_seconds=int(os.getenv("SCAN_INTERVAL_SECONDS", "300")),
            use_order_book=_as_bool(os.getenv("USE_ORDER_BOOK"), True),
            request_delay_seconds=float(os.getenv("REQUEST_DELAY_SECONDS", "1.05")),
            network_retry_attempts=int(os.getenv("NETWORK_RETRY_ATTEMPTS", "4")),
            network_retry_base_seconds=float(os.getenv("NETWORK_RETRY_BASE_SECONDS", "2.0")),
            network_error_retry_seconds=int(os.getenv("NETWORK_ERROR_RETRY_SECONDS", "60")),
            min_price=float(os.getenv("MIN_PRICE", "0.20")),
            max_price=float(os.getenv("MAX_PRICE", "50.00")),
            min_dollar_volume_5m=float(os.getenv("MIN_DOLLAR_VOLUME_5M", "25000")),
            database_path=Path(os.getenv("DATABASE_PATH", "data/scanner.db")),
            latest_csv_path=Path(os.getenv("LATEST_CSV_PATH", "output/latest_candidates.csv")),
            model_path=Path(os.getenv("MODEL_PATH", "models/momentum_model.joblib")),
            learning_enabled=_as_bool(os.getenv("LEARNING_ENABLED"), True),
            learning_horizon_hours=float(os.getenv("LEARNING_HORIZON_HOURS", "24")),
            learning_thresholds=thresholds,
            learning_primary_threshold=round(primary, 6),
            learning_track_top_n=int(os.getenv("LEARNING_TRACK_TOP_N", "50")),
            learning_event_gap_minutes=int(os.getenv("LEARNING_EVENT_GAP_MINUTES", "120")),
            learning_finalize_per_scan=int(os.getenv("LEARNING_FINALIZE_PER_SCAN", "30")),
            learning_refresh_symbols_per_scan=int(os.getenv("LEARNING_REFRESH_SYMBOLS_PER_SCAN", "30")),
            learning_min_bars_per_event=int(os.getenv("LEARNING_MIN_BARS_PER_EVENT", "8")),
            learning_retrain_hours=int(os.getenv("LEARNING_RETRAIN_HOURS", "24")),
            learning_min_rows=int(os.getenv("LEARNING_MIN_ROWS", "200")),
            learning_min_positives=int(os.getenv("LEARNING_MIN_POSITIVES", "20")),
            learning_min_new_labels=int(os.getenv("LEARNING_MIN_NEW_LABELS", "40")),
            learning_min_ap_improvement=float(os.getenv("LEARNING_MIN_AP_IMPROVEMENT", "0.005")),
            learning_max_brier_degradation=float(os.getenv("LEARNING_MAX_BRIER_DEGRADATION", "0.02")),
            learning_min_lift_over_base=float(os.getenv("LEARNING_MIN_LIFT_OVER_BASE", "1.10")),
        )

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.latest_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        Path(self.webull_token_dir).mkdir(parents=True, exist_ok=True)

    def validate_live(self) -> None:
        if not self.webull_app_key or not self.webull_app_secret:
            raise ValueError(
                "Für SCANNER_MODE=webull müssen WEBULL_APP_KEY und "
                "WEBULL_APP_SECRET in .env gesetzt sein."
            )
