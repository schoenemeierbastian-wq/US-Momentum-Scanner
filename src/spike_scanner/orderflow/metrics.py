from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from spike_scanner.orderflow.models import OrderflowMeasurement, ScannerContext


LIQUIDITY_LIQUID_MAX_BPS = 20.0
LIQUIDITY_RESTRICTED_MAX_BPS = 75.0
LIQUIDITY_ILLIQUID_MAX_BPS = 200.0


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _normalise_levels(
    levels: Any,
    *,
    side: str,
    depth: int,
) -> list[dict[str, float]]:
    if not isinstance(levels, list):
        return []

    parsed: list[dict[str, float]] = []
    for row in levels:
        if not isinstance(row, dict):
            continue
        price = _finite_number(row.get("price", row.get("p")))
        size = _finite_number(
            row.get("size", row.get("quantity", row.get("qty", row.get("volume"))))
        )
        if price is None or size is None or price <= 0 or size < 0:
            continue
        parsed.append({"price": price, "size": size})

    reverse = side == "bids"
    parsed.sort(key=lambda row: row["price"], reverse=reverse)

    # Gleiche Preisstufen zusammenführen. Das verhindert Doppelzählung bei
    # uneinheitlichen API-Antworten.
    merged: dict[float, float] = {}
    for row in parsed:
        merged[row["price"]] = merged.get(row["price"], 0.0) + row["size"]
    ordered = [
        {"price": price, "size": size}
        for price, size in sorted(merged.items(), reverse=reverse)
    ]
    return ordered[: max(1, int(depth))]


def _imbalance(bid_size: float, ask_size: float) -> float | None:
    total = bid_size + ask_size
    if total <= 0:
        return None
    return (bid_size - ask_size) / total


def classify_liquidity(spread_bps: float | None) -> tuple[str, str]:
    """Ordnet die Handelbarkeit nur anhand des relativen Spreads ein.

    Das ist bewusst ein konservativer Sicherheitsfilter. Eine Aktie kann trotz
    engem Spread andere Risiken haben; ein extrem breiter Spread wird dagegen
    unabhängig vom Momentum-Score deutlich markiert.
    """

    if spread_bps is None or not math.isfinite(spread_bps) or spread_bps < 0:
        return "UNBEKANNT", "Spread konnte nicht zuverlässig bestimmt werden."
    if spread_bps <= LIQUIDITY_LIQUID_MAX_BPS:
        return "LIQUIDE", f"Spread {spread_bps:.1f} bp liegt im liquiden Bereich."
    if spread_bps <= LIQUIDITY_RESTRICTED_MAX_BPS:
        return "EINGESCHRÄNKT", f"Spread {spread_bps:.1f} bp erfordert vorsichtige Ausführung."
    if spread_bps <= LIQUIDITY_ILLIQUID_MAX_BPS:
        return "ILLIQUIDE", f"Spread {spread_bps:.1f} bp signalisiert hohes Slippage-Risiko."
    return "NICHT HANDELBAR", f"Spread {spread_bps:.1f} bp ist für den Monitor zu breit."


def measure_order_book(
    order_book: dict[str, Any] | None,
    *,
    context: ScannerContext,
    provider: str,
    depth: int = 1,
    timestamp: str | None = None,
) -> OrderflowMeasurement:
    """Berechnet reproduzierbare Level-2-Kennzahlen aus einem Snapshot.

    Es wird bewusst keine Kaufempfehlung erzeugt. ``pressure`` beschreibt nur
    das sichtbare Verhältnis im aktuellen Orderbuch-Snapshot.
    """

    measured_at = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
    requested_depth = max(1, int(depth))
    payload = order_book if isinstance(order_book, dict) else {}
    bids = _normalise_levels(payload.get("bids"), side="bids", depth=requested_depth)
    asks = _normalise_levels(payload.get("asks"), side="asks", depth=requested_depth)

    base = dict(
        symbol=context.symbol.upper(),
        timestamp=measured_at,
        provider=provider,
        depth_levels=min(requested_depth, max(len(bids), len(asks))),
        scan_run_id=context.run_id,
        scan_rank=context.rank,
        scanner_score=context.scanner_score,
        reference_price=context.reference_price,
        raw_book={"bids": bids, "asks": asks},
    )

    if not bids or not asks:
        return OrderflowMeasurement(
            **base,
            data_quality="UNBRAUCHBAR",
            pressure="NICHT VERFÜGBAR",
            liquidity_status="UNBEKANNT",
            liquidity_reason="Bid- oder Ask-Seite fehlt.",
            warnings=["Mindestens eine Bid- und eine Ask-Stufe werden benötigt."],
        )

    best_bid = bids[0]
    best_ask = asks[0]
    if best_ask["price"] <= best_bid["price"]:
        return OrderflowMeasurement(
            **base,
            data_quality="UNBRAUCHBAR",
            pressure="NICHT VERFÜGBAR",
            liquidity_status="UNBEKANNT",
            liquidity_reason="Gekreuztes oder ungültiges Orderbuch.",
            best_bid=best_bid["price"],
            best_ask=best_ask["price"],
            warnings=["Gekreuztes oder ungültiges Orderbuch im Snapshot."],
        )

    midpoint = (best_bid["price"] + best_ask["price"]) / 2.0
    spread_abs = best_ask["price"] - best_bid["price"]
    spread_pct = spread_abs / midpoint
    spread_bps = spread_pct * 10_000.0
    liquidity_status, liquidity_reason = classify_liquidity(spread_bps)

    weights = [1.0 / (index + 1.0) for index in range(requested_depth)]
    weighted_bid = sum(row["size"] * weights[index] for index, row in enumerate(bids))
    weighted_ask = sum(row["size"] * weights[index] for index, row in enumerate(asks))
    book_imbalance = _imbalance(weighted_bid, weighted_ask)
    top_imbalance = _imbalance(best_bid["size"], best_ask["size"])

    top_total = best_bid["size"] + best_ask["size"]
    microprice = midpoint
    if top_total > 0:
        # Bei stärkerem Bid verschiebt sich der Microprice zur Ask-Seite und umgekehrt.
        microprice = (
            best_ask["price"] * best_bid["size"]
            + best_bid["price"] * best_ask["size"]
        ) / top_total
    microprice_edge_bps = ((microprice - midpoint) / midpoint) * 10_000.0

    warnings: list[str] = []
    quality = "OK"
    if len(bids) < requested_depth or len(asks) < requested_depth:
        quality = "TEILWEISE"
        warnings.append(
            f"Snapshot enthält weniger als {requested_depth} Stufen auf mindestens einer Seite."
        )
    if liquidity_status == "EINGESCHRÄNKT":
        warnings.append("Eingeschränkte Liquidität; Limit-Order und kleine Positionsgröße prüfen.")
    elif liquidity_status == "ILLIQUIDE":
        warnings.append("Illiquide; hohes Slippage- und Ausführungsrisiko.")
    elif liquidity_status == "NICHT HANDELBAR":
        warnings.append("Nicht handelbar nach aktuellem Spread-Sicherheitsfilter.")
    if weighted_bid + weighted_ask <= 0:
        quality = "UNBRAUCHBAR"
        warnings.append("Keine sichtbare Größe im ausgewerteten Orderbuch.")

    if book_imbalance is None:
        pressure = "NICHT VERFÜGBAR"
    elif book_imbalance >= 0.20 and microprice_edge_bps >= 0:
        pressure = "BID-DOMINANT"
    elif book_imbalance <= -0.20 and microprice_edge_bps <= 0:
        pressure = "ASK-DOMINANT"
    else:
        pressure = "AUSGEGLICHEN"

    return OrderflowMeasurement(
        **base,
        data_quality=quality,
        pressure=pressure,
        liquidity_status=liquidity_status,
        liquidity_reason=liquidity_reason,
        best_bid=best_bid["price"],
        best_ask=best_ask["price"],
        midpoint=midpoint,
        spread_abs=spread_abs,
        spread_pct=spread_pct,
        spread_bps=spread_bps,
        weighted_bid_depth=weighted_bid,
        weighted_ask_depth=weighted_ask,
        book_imbalance=book_imbalance,
        top_imbalance=top_imbalance,
        microprice=microprice,
        microprice_edge_bps=microprice_edge_bps,
        warnings=warnings,
    )
