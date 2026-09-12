from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class DevelopmentResult:
    """Konservative Veraenderungsbeschreibung zweier Orderbuch-Snapshots.

    Die Auswertung beschreibt nur sichtbare Top-of-Book-Aenderungen. Sie ist
    weder eine Kaufempfehlung noch ein Ersatz fuer Time & Sales / Trade-Delta.
    """

    label: str
    details: str


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _relative_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous


def _valid(row: dict[str, Any]) -> bool:
    return str(row.get("data_quality") or "").upper() not in {
        "FEHLER",
        "UNBRAUCHBAR",
    }


def compare_measurements(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> DevelopmentResult:
    """Vergleicht eine Messung mit der vorherigen gueltigen Messung des Symbols."""

    if not _valid(current):
        return DevelopmentResult("NICHT AUSWERTBAR", "Aktuelle Messung ist fehlerhaft.")
    if previous is None or not _valid(previous):
        return DevelopmentResult("ERSTE MESSUNG", "Noch keine gueltige Vergleichsmessung vorhanden.")

    current_spread = _number(current.get("spread_bps"))
    previous_spread = _number(previous.get("spread_bps"))
    current_imbalance = _number(current.get("book_imbalance"))
    previous_imbalance = _number(previous.get("book_imbalance"))
    current_bid = _number(current.get("weighted_bid_depth"))
    previous_bid = _number(previous.get("weighted_bid_depth"))
    current_ask = _number(current.get("weighted_ask_depth"))
    previous_ask = _number(previous.get("weighted_ask_depth"))

    spread_change = _relative_change(current_spread, previous_spread)
    bid_change = _relative_change(current_bid, previous_bid)
    ask_change = _relative_change(current_ask, previous_ask)
    imbalance_change = (
        current_imbalance - previous_imbalance
        if current_imbalance is not None and previous_imbalance is not None
        else None
    )

    current_pressure = str(current.get("pressure") or "NICHT VERFUEGBAR")
    previous_pressure = str(previous.get("pressure") or "NICHT VERFUEGBAR")
    pressure_flip = {
        current_pressure,
        previous_pressure,
    } == {"BID-DOMINANT", "ASK-DOMINANT"}

    details_parts: list[str] = []
    if spread_change is not None:
        details_parts.append(f"Spread {spread_change:+.0%}")
    if imbalance_change is not None:
        details_parts.append(f"Imbalance {imbalance_change:+.3f}")
    if bid_change is not None:
        details_parts.append(f"Bid {bid_change:+.0%}")
    if ask_change is not None:
        details_parts.append(f"Ask {ask_change:+.0%}")
    details = ", ".join(details_parts) or "Zu wenige vergleichbare Kennzahlen."

    # Ein direkter Wechsel zwischen Bid- und Ask-Dominanz ist bei Tiefe 1 ein
    # starkes Instabilitaetszeichen und wird vor positiven Labels priorisiert.
    extreme_size_swing = any(
        change is not None and (change >= 1.50 or change <= -0.70)
        for change in (bid_change, ask_change)
    )
    if pressure_flip or (
        imbalance_change is not None and abs(imbalance_change) >= 0.75
    ) or extreme_size_swing:
        return DevelopmentResult("ORDERBUCH INSTABIL", details)

    # Spread-Warnungen werden nur bei materieller Aenderung ausgeloest. So wird
    # ein ohnehin breiter, aber unveraenderter Spread nicht jedes Mal als Trend
    # fehlinterpretiert.
    spread_delta_bps = (
        current_spread - previous_spread
        if current_spread is not None and previous_spread is not None
        else None
    )
    if (
        spread_change is not None
        and spread_delta_bps is not None
        and spread_change >= 0.25
        and spread_delta_bps >= 10.0
    ):
        return DevelopmentResult("SPREAD VERSCHLECHTERT", details)
    if (
        spread_change is not None
        and spread_delta_bps is not None
        and spread_change <= -0.20
        and spread_delta_bps <= -10.0
    ):
        return DevelopmentResult("SPREAD VERBESSERT", details)

    buy_pressure_up = (
        imbalance_change is not None
        and imbalance_change >= 0.20
        and (
            (bid_change is not None and bid_change >= 0.25)
            or (ask_change is not None and ask_change <= -0.20)
        )
    )
    sell_pressure_up = (
        imbalance_change is not None
        and imbalance_change <= -0.20
        and (
            (ask_change is not None and ask_change >= 0.25)
            or (bid_change is not None and bid_change <= -0.20)
        )
    )
    if buy_pressure_up:
        return DevelopmentResult("KAUFDRUCK NIMMT ZU", details)
    if sell_pressure_up:
        return DevelopmentResult("VERKAUFSDRUCK NIMMT ZU", details)

    stable_imbalance = imbalance_change is None or abs(imbalance_change) <= 0.12
    stable_spread = spread_change is None or abs(spread_change) <= 0.15
    if current_pressure == previous_pressure and stable_imbalance and stable_spread:
        if current_pressure == "BID-DOMINANT":
            return DevelopmentResult("STABIL BID-DOMINANT", details)
        if current_pressure == "ASK-DOMINANT":
            return DevelopmentResult("STABIL ASK-DOMINANT", details)
        return DevelopmentResult("WEITGEHEND STABIL", details)

    if current_imbalance is not None and abs(current_imbalance) < 0.20:
        return DevelopmentResult("AUSGEGLICHEN", details)
    return DevelopmentResult("LEICHTE VERAENDERUNG", details)


def analyse_history(rows: Iterable[dict[str, Any]]) -> dict[Any, DevelopmentResult]:
    """Erzeugt je Datenbankzeile eine Entwicklung zur vorherigen Messung.

    ``latest_measurements`` liefert normalerweise neueste Zeilen zuerst. Die
    Funktion sortiert deshalb intern chronologisch anhand von Timestamp und ID.
    Fehlerzeilen werden angezeigt, ersetzen aber nicht die letzte gueltige
    Vergleichsmessung eines Symbols.
    """

    materialized = list(rows)
    chronological = sorted(
        materialized,
        key=lambda row: (str(row.get("timestamp") or ""), int(row.get("id") or 0)),
    )
    previous_by_symbol: dict[str, dict[str, Any]] = {}
    results: dict[Any, DevelopmentResult] = {}

    for row in chronological:
        symbol = str(row.get("symbol") or "").upper()
        key = row.get("id")
        previous = previous_by_symbol.get(symbol)
        results[key] = compare_measurements(row, previous)
        if _valid(row):
            previous_by_symbol[symbol] = row

    return results
