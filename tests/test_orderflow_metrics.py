import pytest

from spike_scanner.orderflow import ScannerContext, measure_order_book


def test_bid_dominant_snapshot_is_measured_without_generating_trade_delta() -> None:
    book = {
        "bids": [
            {"price": 10.00, "size": 5000},
            {"price": 9.99, "size": 3000},
        ],
        "asks": [
            {"price": 10.02, "size": 1000},
            {"price": 10.03, "size": 800},
        ],
    }
    result = measure_order_book(
        book,
        context=ScannerContext("ABC", scanner_score=82, reference_price=10.01),
        provider="mock",
        depth=2,
        timestamp="2026-01-01T12:00:00+00:00",
    )

    assert result.data_quality == "OK"
    assert result.pressure == "BID-DOMINANT"
    assert result.book_imbalance is not None and result.book_imbalance > 0.5
    assert result.microprice_edge_bps is not None and result.microprice_edge_bps > 0
    assert result.trade_delta_ratio is None


def test_crossed_book_is_rejected() -> None:
    result = measure_order_book(
        {"bids": [{"price": 10.02, "size": 100}], "asks": [{"price": 10.01, "size": 100}]},
        context=ScannerContext("XYZ"),
        provider="mock",
    )
    assert result.data_quality == "UNBRAUCHBAR"
    assert result.pressure == "NICHT VERFÜGBAR"
    assert result.spread_bps is None


def test_levels_are_sorted_and_duplicate_prices_are_merged() -> None:
    result = measure_order_book(
        {
            "bids": [
                {"price": 9.99, "size": 100},
                {"price": 10.00, "size": 200},
                {"price": 10.00, "size": 300},
            ],
            "asks": [{"price": 10.03, "size": 100}, {"price": 10.02, "size": 200}],
        },
        context=ScannerContext("SORT"),
        provider="mock",
        depth=2,
    )
    assert result.best_bid == pytest.approx(10.00)
    assert result.best_ask == pytest.approx(10.02)
    assert result.raw_book["bids"][0]["size"] == pytest.approx(500)


def test_liquidity_status_uses_conservative_spread_bands() -> None:
    liquid = measure_order_book(
        {"bids": [{"price": 100.00, "size": 100}], "asks": [{"price": 100.10, "size": 100}]},
        context=ScannerContext("LIQ"),
        provider="mock",
        depth=1,
    )
    restricted = measure_order_book(
        {"bids": [{"price": 10.00, "size": 100}], "asks": [{"price": 10.05, "size": 100}]},
        context=ScannerContext("RES"),
        provider="mock",
        depth=1,
    )
    illiquid = measure_order_book(
        {"bids": [{"price": 10.00, "size": 100}], "asks": [{"price": 10.15, "size": 100}]},
        context=ScannerContext("ILL"),
        provider="mock",
        depth=1,
    )
    blocked = measure_order_book(
        {"bids": [{"price": 1.00, "size": 100}], "asks": [{"price": 1.10, "size": 100}]},
        context=ScannerContext("BLOCK"),
        provider="mock",
        depth=1,
    )

    assert liquid.liquidity_status == "LIQUIDE"
    assert restricted.liquidity_status == "EINGESCHRÄNKT"
    assert illiquid.liquidity_status == "ILLIQUIDE"
    assert blocked.liquidity_status == "NICHT HANDELBAR"
