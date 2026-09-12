from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from spike_scanner.models import UniverseItem


class MockMarketDataProvider:
    """Deterministische Testdaten mit einigen künstlichen Momentum-Aktien."""

    name = "mock"

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed
        self.symbols = [
            "ALFA", "BETA", "CYGN", "DASH", "ELIO", "FARO", "GIGA", "HELI",
            "IONX", "JOLT", "KITE", "LUMA", "MESA", "NOVA", "ORCA", "PICO",
            "QBIT", "RUSH", "SOLA", "TRIX", "URSA", "VOLT", "WAVE", "XENO",
            "YARD", "ZETA",
        ]

    def discover_universe(self, limit: int) -> list[UniverseItem]:
        items: list[UniverseItem] = []
        for idx, symbol in enumerate(self.symbols[:limit]):
            hotness = max(0.0, 1.0 - idx / 8.0)
            items.append(
                UniverseItem(
                    symbol=symbol,
                    name=f"Mock {symbol}",
                    source_rank=idx + 1,
                    change_ratio=0.02 + hotness * 0.35,
                    relative_volume=1.0 + hotness * 14.0,
                    volume=200_000 + hotness * 8_000_000,
                    last_price=1.5 + idx * 0.35,
                )
            )
        return items

    def get_bars(self, symbol: str, count: int) -> pd.DataFrame:
        idx = self.symbols.index(symbol) if symbol in self.symbols else 20
        rng = np.random.default_rng(self.seed + idx)
        count = max(count, 40)
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        timestamps = [end - timedelta(minutes=count - 1 - i) for i in range(count)]

        base_price = 1.2 + idx * 0.32
        noise = rng.normal(0, 0.004 + idx * 0.0001, count)
        drift = np.zeros(count)
        volume_multiplier = np.ones(count)

        if idx < 3:
            start = int(count * 0.68)
            drift[start:] = np.linspace(0.002, 0.026 - idx * 0.004, count - start)
            volume_multiplier[start:] = np.linspace(2.0, 18.0 - idx * 3.0, count - start)
        elif idx < 7:
            start = int(count * 0.80)
            drift[start:] = np.linspace(0.001, 0.008, count - start)
            volume_multiplier[start:] = np.linspace(1.5, 5.0, count - start)

        returns = noise + drift
        close = base_price * np.cumprod(1 + returns)
        open_ = np.r_[base_price, close[:-1]]
        spread = np.maximum(close * rng.uniform(0.001, 0.008, count), 0.002)
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        base_volume = rng.lognormal(mean=9.0, sigma=0.45, size=count)
        volume = base_volume * volume_multiplier

        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(timestamps, utc=True),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )

    def get_order_book(self, symbol: str, depth: int = 5) -> dict:
        idx = self.symbols.index(symbol) if symbol in self.symbols else 20
        bars = self.get_bars(symbol, 60)
        price = float(bars.iloc[-1]["close"])
        hot = idx < 3
        spread_pct = 0.0015 if hot else 0.006 + min(idx, 10) * 0.0005
        half = price * spread_pct / 2
        bid_size = 12_000 if hot else 3_500
        ask_size = 4_000 if hot else 4_500
        return {
            "bids": [
                {"price": price - half - level * half, "size": bid_size / (level + 1)}
                for level in range(depth)
            ],
            "asks": [
                {"price": price + half + level * half, "size": ask_size / (level + 1)}
                for level in range(depth)
            ],
        }
