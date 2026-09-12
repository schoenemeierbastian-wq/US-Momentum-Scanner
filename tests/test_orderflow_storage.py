from spike_scanner.orderflow import (
    OrderflowStorage,
    ScannerContext,
    measure_order_book,
)


def test_orderflow_measurement_uses_separate_table(tmp_path) -> None:
    storage = OrderflowStorage(tmp_path / "scanner.db")
    measurement = measure_order_book(
        {
            "bids": [{"price": 5.00, "size": 1000}],
            "asks": [{"price": 5.02, "size": 800}],
        },
        context=ScannerContext("TEST", run_id="run-1", rank=1, scanner_score=80),
        provider="mock",
        depth=1,
    )
    row_id = storage.save(measurement)
    rows = storage.latest_measurements()

    assert row_id > 0
    assert rows[0]["symbol"] == "TEST"
    assert rows[0]["scan_run_id"] == "run-1"
    assert rows[0]["scanner_score"] == 80


def test_existing_database_is_migrated_with_liquidity_columns(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as con:
        con.execute(
            """
            CREATE TABLE orderflow_measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                provider TEXT NOT NULL,
                scan_run_id TEXT,
                scan_rank INTEGER,
                scanner_score REAL,
                reference_price REAL,
                depth_levels INTEGER NOT NULL,
                data_quality TEXT NOT NULL,
                pressure TEXT NOT NULL,
                best_bid REAL,
                best_ask REAL,
                midpoint REAL,
                spread_abs REAL,
                spread_pct REAL,
                spread_bps REAL,
                weighted_bid_depth REAL,
                weighted_ask_depth REAL,
                book_imbalance REAL,
                top_imbalance REAL,
                microprice REAL,
                microprice_edge_bps REAL,
                trade_delta_ratio REAL,
                aggressive_buy_volume REAL,
                aggressive_sell_volume REAL,
                warnings_json TEXT NOT NULL DEFAULT '[]',
                raw_book_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

    OrderflowStorage(db)
    with sqlite3.connect(db) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(orderflow_measurements)")}
    assert "liquidity_status" in columns
    assert "liquidity_reason" in columns
