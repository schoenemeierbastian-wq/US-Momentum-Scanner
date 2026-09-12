from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from spike_scanner.config import Settings
from spike_scanner.learning import DEFAULT_FEATURES, train_and_compare_model
from spike_scanner.providers.mock_provider import MockMarketDataProvider
from spike_scanner.scanner import MomentumScanner
from spike_scanner.storage import Storage


def test_scan_registers_prospective_learning_events_and_bars(tmp_path):
    settings = Settings(
        mode="mock",
        universe_size=8,
        top_n=3,
        database_path=tmp_path / "scanner.db",
        latest_csv_path=tmp_path / "latest.csv",
        model_path=tmp_path / "model.joblib",
        min_dollar_volume_5m=1,
        learning_track_top_n=8,
        learning_min_rows=9999,
    )
    storage = Storage(settings.database_path)
    result = MomentumScanner(settings, MockMarketDataProvider(), storage).scan_once()

    counts = storage.learning_counts(24)
    assert result.learning_summary["registered"] >= 3
    assert counts["pending"] >= 3
    with storage.connect() as con:
        bar_count = con.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
    assert bar_count > 0


def test_due_event_is_finalized_from_stored_market_bars(tmp_path):
    settings = Settings(
        mode="mock",
        universe_size=5,
        top_n=3,
        database_path=tmp_path / "scanner.db",
        latest_csv_path=tmp_path / "latest.csv",
        model_path=tmp_path / "model.joblib",
        min_dollar_volume_5m=1,
        learning_track_top_n=5,
        learning_min_rows=9999,
        learning_min_bars_per_event=4,
    )
    storage = Storage(settings.database_path)
    scanner = MomentumScanner(settings, MockMarketDataProvider(), storage)
    scanner.scan_once()

    now = datetime.now(timezone.utc)
    signal = now - timedelta(hours=24, minutes=5)
    due = now - timedelta(minutes=5)
    with storage.connect() as con:
        event = con.execute(
            "SELECT id, symbol FROM learning_events ORDER BY id LIMIT 1"
        ).fetchone()
        con.execute(
            "UPDATE learning_events SET signal_timestamp=?, due_at=? WHERE id=?",
            (signal.isoformat(), due.isoformat(), int(event["id"])),
        )

    timestamps = pd.date_range(signal + timedelta(minutes=1), due, periods=12, tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [1.0] * 12,
            "high": [1.0 + i * 0.02 for i in range(12)],
            "low": [0.98] * 12,
            "close": [1.0 + i * 0.01 for i in range(12)],
            "volume": [1000] * 12,
        }
    )
    storage.save_market_bars(str(event["symbol"]), bars)
    summary = scanner.run_learning_cycle(force_training=False)

    assert summary["finalized"] >= 1
    finalized = storage.learning_events_frame(status="finalized", horizon_hours=24)
    assert not finalized.empty
    assert finalized.iloc[0]["data_quality"] == "usable"


def test_challenger_model_is_saved_only_after_temporal_evaluation(tmp_path):
    storage = Storage(tmp_path / "scanner.db")
    start = datetime.now(timezone.utc) - timedelta(days=80)
    with storage.connect() as con:
        con.execute(
            "INSERT INTO scan_runs(run_id,timestamp,mode,universe_count,error_json) "
            "VALUES (?,?,?,?,?)",
            ("run", start.isoformat(), "mock", 1, "[]"),
        )
        for i in range(240):
            positive = int(i % 5 == 0)
            features = {name: 0.0 for name in DEFAULT_FEATURES}
            features["ret_5m"] = 0.15 if positive else -0.01
            features["relative_volume_5m"] = 12.0 if positive else 1.1
            features["screener_change_ratio"] = 0.35 if positive else 0.01
            signal = start + timedelta(hours=i * 6)
            future_return = 0.30 if positive else 0.03
            con.execute(
                """
                INSERT INTO learning_events
                (event_key, source_run_id, signal_timestamp, due_at, horizon_hours,
                 symbol, entry_price, signal_score, risk_score, source_rank,
                 features_json, reasons_json, status, finalized_at, max_high,
                 min_low, final_price, max_return, max_drawdown, bars_found,
                 data_quality, labels_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"event-{i}",
                    "run",
                    signal.isoformat(),
                    (signal + timedelta(hours=24)).isoformat(),
                    24.0,
                    f"S{i % 20}",
                    1.0,
                    80.0,
                    20.0,
                    i % 50 + 1,
                    json.dumps(features),
                    "[]",
                    "finalized",
                    (signal + timedelta(hours=25)).isoformat(),
                    1.0 + future_return,
                    0.95,
                    1.02,
                    future_return,
                    -0.05,
                    100,
                    "usable",
                    json.dumps({"0.200000": positive}),
                ),
            )

    settings = Settings(
        database_path=storage.path,
        model_path=tmp_path / "model.joblib",
        learning_horizon_hours=24,
        learning_thresholds=(0.20,),
        learning_primary_threshold=0.20,
        learning_min_rows=100,
        learning_min_positives=20,
        learning_min_lift_over_base=1.0,
    )
    metrics = train_and_compare_model(storage, settings, 0.20)

    assert metrics["accepted"] is True
    assert metrics["average_precision"] > metrics["base_rate_test"]
    assert (tmp_path / "model_h24_t020.joblib").exists()
