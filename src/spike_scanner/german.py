from __future__ import annotations

import math
from typing import Any


FEATURE_LABELS: dict[str, str] = {
    "price": "Aktueller Kurs",
    "ret_1m": "Kursänderung 1 Minute",
    "ret_5m": "Kursänderung 5 Minuten",
    "ret_15m": "Kursänderung 15 Minuten",
    "ret_30m": "Kursänderung 30 Minuten",
    "momentum_acceleration": "Momentum-Beschleunigung",
    "relative_volume_5m": "Relatives Volumen (5 Min.)",
    "volume_z_20": "Volumen-Abweichung (Z-Wert)",
    "vwap_distance": "Abstand zum VWAP",
    "breakout_20": "Ausbruch über 20-Minuten-Hoch",
    "range_position": "Position in der Tagesspanne",
    "realized_volatility_20": "Realisierte Volatilität (20 Min.)",
    "downside_volatility_20": "Abwärtsvolatilität (20 Min.)",
    "dollar_volume_5m": "Dollarvolumen (5 Min.)",
    "bar_range_pct": "Spanne der letzten Kerze",
    "close_location": "Schlussposition in letzter Kerze",
    "screener_change_ratio": "Tagesänderung laut Screener",
    "screener_relative_volume": "Relatives Volumen laut Screener",
    "screener_volume": "Gesamtvolumen laut Screener",
    "spread_pct": "Bid/Ask-Spread",
    "book_imbalance": "Orderbuch-Ungleichgewicht",
    "book_depth": "Gewichtete Orderbuchtiefe",
}

PERCENT_FEATURES = {
    "ret_1m",
    "ret_5m",
    "ret_15m",
    "ret_30m",
    "momentum_acceleration",
    "vwap_distance",
    "breakout_20",
    "range_position",
    "realized_volatility_20",
    "downside_volatility_20",
    "bar_range_pct",
    "close_location",
    "screener_change_ratio",
    "spread_pct",
    "book_imbalance",
}

INTEGER_FEATURES = {"screener_volume", "book_depth"}


def de_number(value: Any, decimals: int = 2) -> str:
    """Formatiert Zahlen ohne Abhängigkeit von der Windows-Locale."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "–"
    if not math.isfinite(number):
        return "–"
    text = f"{number:,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def format_feature(name: str, value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if name in PERCENT_FEATURES:
        return f"{de_number(number * 100, 2)} %"
    if name == "price":
        return f"{de_number(number, 4)} USD"
    if name == "dollar_volume_5m":
        return f"{de_number(number, 0)} USD"
    if name in INTEGER_FEATURES:
        return de_number(number, 0)
    return de_number(number, 3)


def feature_label(name: str) -> str:
    return FEATURE_LABELS.get(name, name.replace("_", " ").capitalize())


def probability_text(value: Any) -> str:
    if value is None:
        return "Noch nicht verfügbar"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "–"
    if not math.isfinite(number):
        return "Noch nicht verfügbar"
    return f"{de_number(number * 100, 2)} %"
