from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from spike_scanner.models import UniverseItem


REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if not math.isfinite(denominator) or abs(denominator) < 1e-12:
        return default
    value = numerator / denominator
    return float(value) if math.isfinite(value) else default


def _return(close: pd.Series, periods: int) -> float:
    if len(close) <= periods:
        return 0.0
    return _safe_div(float(close.iloc[-1] - close.iloc[-1 - periods]), float(close.iloc[-1 - periods]))


def _order_book_features(order_book: dict[str, Any] | None) -> dict[str, float]:
    if not order_book:
        return {"spread_pct": 0.0, "book_imbalance": 0.0, "book_depth": 0.0}
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not bids or not asks:
        return {"spread_pct": 0.0, "book_imbalance": 0.0, "book_depth": 0.0}

    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    mid = (best_bid + best_ask) / 2
    spread_pct = _safe_div(best_ask - best_bid, mid)

    weights = np.array([1.0 / (i + 1) for i in range(max(len(bids), len(asks)))])
    bid_sizes = np.array([float(x.get("size", 0.0)) for x in bids])
    ask_sizes = np.array([float(x.get("size", 0.0)) for x in asks])
    weighted_bid = float(np.sum(bid_sizes * weights[: len(bid_sizes)]))
    weighted_ask = float(np.sum(ask_sizes * weights[: len(ask_sizes)]))
    imbalance = _safe_div(weighted_bid - weighted_ask, weighted_bid + weighted_ask)
    return {
        "spread_pct": spread_pct,
        "book_imbalance": imbalance,
        "book_depth": weighted_bid + weighted_ask,
    }


def compute_features(
    bars: pd.DataFrame,
    universe_item: UniverseItem,
    order_book: dict[str, Any] | None = None,
) -> dict[str, float]:
    missing = [col for col in REQUIRED_COLUMNS if col not in bars.columns]
    if missing:
        raise ValueError(f"Fehlende Bar-Spalten: {missing}")
    if len(bars) < 20:
        raise ValueError("Mindestens 20 Minutenbalken werden benötigt")

    frame = bars.copy().sort_values("timestamp").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close", "volume"])
    if len(frame) < 20:
        raise ValueError("Nach Bereinigung bleiben weniger als 20 Balken")

    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float).clip(lower=0)
    price = float(close.iloc[-1])

    typical = (high + low + close) / 3
    cumulative_volume = volume.cumsum().replace(0, np.nan)
    vwap = (typical * volume).cumsum() / cumulative_volume
    current_vwap = float(vwap.iloc[-1]) if math.isfinite(float(vwap.iloc[-1])) else price

    recent5_volume = float(volume.tail(5).mean())
    baseline_volume = float(volume.iloc[-25:-5].mean()) if len(volume) >= 25 else float(volume.iloc[:-5].mean())
    volume_std = float(volume.iloc[-25:-5].std(ddof=0)) if len(volume) >= 25 else float(volume.iloc[:-5].std(ddof=0))
    rel_volume_5m = _safe_div(recent5_volume, baseline_volume, 1.0)
    volume_z = _safe_div(recent5_volume - baseline_volume, volume_std, 0.0)

    previous_high_20 = float(high.iloc[-21:-1].max()) if len(high) >= 21 else float(high.iloc[:-1].max())
    breakout_20 = _safe_div(price - previous_high_20, previous_high_20)
    intraday_low = float(low.min())
    intraday_high = float(high.max())
    range_position = _safe_div(price - intraday_low, intraday_high - intraday_low, 0.5)

    one_min_returns = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    realized_vol_20 = float(one_min_returns.tail(20).std(ddof=0)) if len(one_min_returns) else 0.0
    downside_vol_20 = float(one_min_returns.tail(20).clip(upper=0).std(ddof=0)) if len(one_min_returns) else 0.0

    ret_1m = _return(close, 1)
    ret_5m = _return(close, 5)
    ret_15m = _return(close, 15)
    ret_30m = _return(close, 30)
    prior_ret_5m = 0.0
    if len(close) > 10:
        prior_ret_5m = _safe_div(float(close.iloc[-6] - close.iloc[-11]), float(close.iloc[-11]))

    dollar_volume_5m = float((close.tail(5) * volume.tail(5)).sum())
    bar_range_pct = _safe_div(float(high.iloc[-1] - low.iloc[-1]), price)
    close_location = _safe_div(
        float(close.iloc[-1] - low.iloc[-1]),
        float(high.iloc[-1] - low.iloc[-1]),
        0.5,
    )

    features = {
        "price": price,
        "ret_1m": ret_1m,
        "ret_5m": ret_5m,
        "ret_15m": ret_15m,
        "ret_30m": ret_30m,
        "momentum_acceleration": ret_5m - prior_ret_5m,
        "relative_volume_5m": rel_volume_5m,
        "volume_z_20": volume_z,
        "vwap_distance": _safe_div(price - current_vwap, current_vwap),
        "breakout_20": breakout_20,
        "range_position": range_position,
        "realized_volatility_20": realized_vol_20,
        "downside_volatility_20": downside_vol_20,
        "dollar_volume_5m": dollar_volume_5m,
        "bar_range_pct": bar_range_pct,
        "close_location": close_location,
        "screener_change_ratio": float(universe_item.change_ratio or 0.0),
        "screener_relative_volume": float(universe_item.relative_volume or 0.0),
        "screener_volume": float(universe_item.volume or 0.0),
    }
    features.update(_order_book_features(order_book))
    return {key: float(value) if math.isfinite(float(value)) else 0.0 for key, value in features.items()}
