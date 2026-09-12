from __future__ import annotations

from datetime import datetime, timedelta, timezone

from spike_scanner.models import Candidate, ScanResult
from spike_scanner.storage import Storage


def _candidate(symbol: str, timestamp: str, rank: int, price: float = 1.0) -> Candidate:
    return Candidate(
        symbol=symbol,
        timestamp=timestamp,
        price=price,
        signal_score=80.0,
        risk_score=20.0,
        rank=rank,
    )


def _result(run_id: str, timestamp: str, symbols: list[str]) -> ScanResult:
    observations = [_candidate(symbol, timestamp, index + 1, 1.0 + index) for index, symbol in enumerate(symbols)]
    return ScanResult(
        run_id=run_id,
        timestamp=timestamp,
        mode="mock",
        universe_count=len(observations),
        candidates=observations[:2],
        all_observations=observations,
    )


def test_first_recommendation_is_not_overwritten(tmp_path):
    storage = Storage(tmp_path / "scanner.db")
    start = datetime(2026, 8, 8, 12, 57, 1, tzinfo=timezone.utc)
    t1 = start.isoformat(timespec="seconds")
    t2 = (start + timedelta(minutes=5)).isoformat(timespec="seconds")

    storage.save_scan(_result("run-1", t1, ["AAA", "BBB", "CCC"]))
    first = storage.recommendations_for(["AAA"])["AAA"]
    storage.save_scan(_result("run-2", t2, ["AAA", "BBB", "DDD"]))
    second = storage.recommendations_for(["AAA"])["AAA"]

    assert second["first_seen_at"] == first["first_seen_at"] == t1
    assert second["first_seen_price"] == first["first_seen_price"]
    assert second["last_seen_at"] == t2
    assert second["seen_count"] == 2
    assert second["active"] == 1


def test_reappearing_symbol_starts_new_recommendation_episode(tmp_path):
    storage = Storage(tmp_path / "scanner.db")
    start = datetime(2026, 8, 8, 12, 57, 1, tzinfo=timezone.utc)
    t1 = start.isoformat(timespec="seconds")
    t2 = (start + timedelta(minutes=5)).isoformat(timespec="seconds")
    t3 = (start + timedelta(minutes=10)).isoformat(timespec="seconds")

    storage.save_scan(_result("run-1", t1, ["AAA", "BBB", "CCC"]))
    storage.save_scan(_result("run-2", t2, ["CCC", "DDD", "EEE"]))
    storage.save_scan(_result("run-3", t3, ["AAA", "DDD", "EEE"]))

    history = storage.recommendation_history("AAA")
    assert len(history) == 2
    assert history[0]["first_seen_at"] == t3
    assert history[0]["active"] == 1
    assert history[1]["first_seen_at"] == t1
    assert history[1]["active"] == 0
