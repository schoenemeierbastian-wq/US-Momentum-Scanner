from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class UniverseItem:
    symbol: str
    name: str = ""
    source_rank: int | None = None
    change_ratio: float | None = None
    relative_volume: float | None = None
    volume: float | None = None
    last_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Candidate:
    symbol: str
    timestamp: str
    price: float
    signal_score: float
    risk_score: float
    rank: int = 0
    model_probability: float | None = None
    model_probabilities: dict[str, float] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScanResult:
    run_id: str
    timestamp: str
    mode: str
    universe_count: int
    candidates: list[Candidate]
    all_observations: list[Candidate]
    errors: list[str] = field(default_factory=list)
    learning_summary: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls, mode: str, run_id: str) -> "ScanResult":
        now = datetime.now(timezone.utc).isoformat()
        return cls(run_id, now, mode, 0, [], [], [], {})
