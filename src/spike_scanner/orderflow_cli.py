from __future__ import annotations

import argparse
import json
import logging

from spike_scanner.config import Settings
from spike_scanner.orderflow import OrderflowMonitor, OrderflowStorage, ScannerContext
from spike_scanner.providers import MockMarketDataProvider, WebullMarketDataProvider


def _provider(settings: Settings):
    if settings.mode == "mock":
        return MockMarketDataProvider()
    if settings.mode == "webull":
        settings.validate_live()
        return WebullMarketDataProvider(settings)
    raise ValueError(f"Unbekannter Modus: {settings.mode}")


def _parse_symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return sorted(
        {part.strip().upper() for part in value.replace(";", ",").split(",") if part.strip()}
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Getrenntes Level-2-Messmodul. Es verändert keine Scanner-Scores "
            "und führt keine Orders aus."
        )
    )
    parser.add_argument("--env", default=None, help="Pfad zur lokalen .env-Datei")
    parser.add_argument("--mode", choices=["mock", "webull"], default=None)
    parser.add_argument("--symbols", default=None, help="Kommagetrennte Symbole")
    parser.add_argument("--latest-top", action="store_true", help="Letzte Top-Kandidaten messen")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--depth", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env(args.env)
    if args.mode:
        settings.mode = args.mode
    settings.ensure_directories()

    storage = OrderflowStorage(settings.database_path)
    monitor = OrderflowMonitor(_provider(settings), storage)

    symbols = _parse_symbols(args.symbols)
    contexts: list[ScannerContext]
    if symbols:
        contexts = [ScannerContext(symbol=symbol) for symbol in symbols]
    else:
        contexts = storage.latest_top_contexts(limit=args.limit)
        if not contexts and settings.mode == "mock":
            contexts = [ScannerContext(symbol=symbol) for symbol in ("ALFA", "BETA", "CYGN")]

    if not contexts:
        parser.error(
            "Keine Symbole gefunden. Zuerst den Scanner ausführen oder --symbols AAPL,TSLA angeben."
        )

    measurements = monitor.measure_many(contexts[: max(1, args.limit)], depth=args.depth)
    if args.json:
        print(json.dumps([item.to_dict() for item in measurements], indent=2, ensure_ascii=False))
        return

    print("\nLevel-2-Messung – ohne Einfluss auf den Scanner")
    for item in measurements:
        spread = "–" if item.spread_bps is None else f"{item.spread_bps:.1f} bp"
        imbalance = "–" if item.book_imbalance is None else f"{item.book_imbalance:+.3f}"
        micro = "–" if item.microprice_edge_bps is None else f"{item.microprice_edge_bps:+.2f} bp"
        print(
            f"{item.symbol:<7} {item.pressure:<18} "
            f"Spread {spread:<10} Imbalance {imbalance:<8} Micro {micro:<10} "
            f"Qualität {item.data_quality}"
        )
        for warning in item.warnings:
            print(f"  Warnung: {warning}")


if __name__ == "__main__":
    main()
