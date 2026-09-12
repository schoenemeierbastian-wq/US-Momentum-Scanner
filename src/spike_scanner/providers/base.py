from __future__ import annotations

from typing import Protocol

import pandas as pd

from spike_scanner.models import UniverseItem


class MarketDataProvider(Protocol):
    name: str

    def discover_universe(self, limit: int) -> list[UniverseItem]: ...

    def get_bars(self, symbol: str, count: int) -> pd.DataFrame: ...

    def get_order_book(self, symbol: str, depth: int = 5) -> dict: ...
