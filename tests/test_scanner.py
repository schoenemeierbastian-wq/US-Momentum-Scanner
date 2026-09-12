from spike_scanner.config import Settings
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
