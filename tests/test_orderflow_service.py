from spike_scanner.orderflow import OrderflowMonitor, OrderflowStorage, ScannerContext
from spike_scanner.providers import MockMarketDataProvider


def test_monitor_saves_mock_measurement(tmp_path) -> None:
    storage = OrderflowStorage(tmp_path / "scanner.db")
    monitor = OrderflowMonitor(MockMarketDataProvider(), storage)

    result = monitor.measure(ScannerContext("ALFA"), depth=5)

    assert result.symbol == "ALFA"
    assert result.data_quality in {"OK", "TEILWEISE"}
    assert len(storage.latest_measurements()) == 1
