import pytest
from spike_scanner.config import Settings
from spike_scanner.models import UniverseItem
from spike_scanner.providers.mock_provider import MockMarketDataProvider
from spike_scanner.scanner import MomentumScanner
from spike_scanner.storage import Storage


def test_scanner_returns_top_three(tmp_path):
    settings = Settings(
        mode="mock",
        universe_size=15,
        top_n=3,
        bar_count=120,
        database_path=tmp_path / "scanner.db",
        latest_csv_path=tmp_path / "latest.csv",
        model_path=tmp_path / "model.joblib",
        min_dollar_volume_5m=1,
    )
    result = MomentumScanner(
        settings, MockMarketDataProvider(), Storage(settings.database_path)
    ).scan_once()

    assert len(result.candidates) == 3
    assert result.candidates[0].rank == 1
    assert settings.latest_csv_path.exists()
    assert Storage(settings.database_path).latest_frame().shape[0] >= 3


def test_scanner_aborts_after_three_consecutive_network_errors(tmp_path):
    class FailingProvider:
        name = "webull"

        def __init__(self):
            self.bar_calls = 0

        def discover_universe(self, limit):
            return [
                UniverseItem(symbol=f"TEST{i}", last_price=1.0)
                for i in range(5)
            ]

        def get_bars(self, symbol, count):
            self.bar_calls += 1
            raise ConnectionError("read timed out")

        def get_order_book(self, symbol, depth=1):
            return {}

    settings = Settings(
        mode="mock",
        universe_size=5,
        database_path=tmp_path / "scanner.db",
        latest_csv_path=tmp_path / "latest.csv",
        model_path=tmp_path / "model.joblib",
        learning_enabled=False,
        use_order_book=False,
    )
    provider = FailingProvider()
    scanner = MomentumScanner(
        settings,
        provider,
        Storage(settings.database_path),
    )

    with pytest.raises(ConnectionError, match="read timed out"):
        scanner.scan_once()

    assert provider.bar_calls == 3
