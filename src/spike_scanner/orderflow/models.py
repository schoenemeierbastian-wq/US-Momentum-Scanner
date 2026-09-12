from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class ScannerContext:
    """Optionaler Bezug zu einem bereits gespeicherten Scanner-Kandidaten."""

    symbol: str
    run_id: str | None = None
    rank: int | None = None
    scanner_score: float | None = None
    reference_price: float | None = None


@dataclass(slots=True)
class OrderflowMeasurement:
    """Read-only Level-2-Messung.

    Die Messung verändert weder Signal-, Ranking- noch Kauf-Score des Scanners.
    Trade-Delta bleibt in Phase 1 absichtlich leer, weil dafür echte Tick-/
    Time-and-Sales-Daten benötigt werden.
    """

    symbol: str
    timestamp: str
    provider: str
    depth_levels: int
    data_quality: str
    pressure: str

    scan_run_id: str | None = None
    scan_rank: int | None = None
    scanner_score: float | None = None
    reference_price: float | None = None

    best_bid: float | None = None
    best_ask: float | None = None
    midpoint: float | None = None
    spread_abs: float | None = None
    spread_pct: float | None = None
    spread_bps: float | None = None

    weighted_bid_depth: float | None = None
    weighted_ask_depth: float | None = None
    book_imbalance: float | None = None
    top_imbalance: float | None = None

    microprice: float | None = None
    microprice_edge_bps: float | None = None

    # Separate Liquiditätsbewertung. Sie überschreibt bewusst nicht den
    # bestehenden Momentum-Score und erzeugt keine Kaufempfehlung.
    liquidity_status: str | None = None
    liquidity_reason: str | None = None

    # Reserviert für Phase 2 mit echten Tick-/Time-and-Sales-Daten.
    trade_delta_ratio: float | None = None
    aggressive_buy_volume: float | None = None
    aggressive_sell_volume: float | None = None

    warnings: list[str] = field(default_factory=list)
    raw_book: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
