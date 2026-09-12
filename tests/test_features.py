from spike_scanner.features import compute_features
from spike_scanner.models import UniverseItem
from spike_scanner.providers.mock_provider import MockMarketDataProvider
from spike_scanner.scoring import score_features


def test_hot_mock_symbol_scores_higher_than_cold_symbol():
    provider = MockMarketDataProvider()
    hot_item = UniverseItem("ALFA", change_ratio=0.35, relative_volume=15)
    cold_item = UniverseItem("TRIX", change_ratio=0.02, relative_volume=1.1)

    hot_features = compute_features(
        provider.get_bars("ALFA", 120), hot_item, provider.get_order_book("ALFA")
    )
    cold_features = compute_features(
        provider.get_bars("TRIX", 120), cold_item, provider.get_order_book("TRIX")
    )

    assert score_features(hot_features).signal_score > score_features(cold_features).signal_score
    assert hot_features["relative_volume_5m"] > cold_features["relative_volume_5m"]
