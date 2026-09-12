from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from spike_scanner.config import Settings
from spike_scanner.learning import build_training_frame, train_probability_model
from spike_scanner.providers import MockMarketDataProvider, WebullMarketDataProvider
from spike_scanner.scanner import MomentumScanner
from spike_scanner.scoring import (combined_score, ranking_score, market_phase_at, purchase_assessment)
from spike_scanner.storage import Storage


def _provider(settings: Settings):
    if settings.mode == "mock":
        return MockMarketDataProvider()
    if settings.mode == "webull":
        return WebullMarketDataProvider(settings)
    raise ValueError(f"Unbekannter Modus: {settings.mode}")


def _print_result(result) -> None:
    print(f"\nScan {result.run_id} | Modus: {result.mode} | Universum: {result.universe_count}")
    if result.errors:
        print(f"Fehler/Warnungen: {len(result.errors)}")
    if not result.candidates:
        print("Keine Kandidaten haben die Filter passiert.")
    for candidate in result.candidates:
        probability = (
            f" | Modell-P: {candidate.model_probability:.2%}"
            if candidate.model_probability is not None
            else ""
        )
        assessment = purchase_assessment(
            candidate.signal_score, candidate.risk_score, candidate.model_probability,
            candidate.features, age_minutes=0.0, phase=market_phase_at(candidate.timestamp)
        )
        print(
            f"{candidate.rank}. {candidate.symbol:<6} "
            f"Preis {candidate.price:>9.4f} | Signal {candidate.signal_score:>6.2f} "
            f"| Risiko {candidate.risk_score:>6.2f} "
            f"| Gesamt {combined_score(candidate.signal_score, candidate.risk_score):>6.2f} "
            f"| Ranking {ranking_score(candidate.signal_score, candidate.risk_score, candidate.model_probability):>6.2f} "
            f"| Kauf {assessment.score:>6.2f} ({assessment.quality}){probability}"
        )
        if candidate.model_probabilities:
            targets = ", ".join(
                f"+{float(key):.0%}: {value:.2%}"
                for key, value in sorted(candidate.model_probabilities.items())
            )
            print(f"   Modelle: {targets}")
        print("   " + "; ".join(candidate.reasons))
    if result.learning_summary:
        learn = result.learning_summary
        print(
            "Lernen: "
            f"{learn.get('registered', 0)} neu verfolgt, "
            f"{learn.get('finalized', 0)} ausgewertet, "
            f"{learn.get('models_accepted', 0)} Modell(e) übernommen."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="US Momentum Scanner")
    parser.add_argument("--env", default=None, help="Pfad zu einer .env-Datei")
    parser.add_argument("--mode", choices=["mock", "webull"], default=None)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="Einmaliger Scan")
    scan_parser.add_argument("--top", type=int, default=None)
    scan_parser.add_argument("--universe", type=int, default=None)

    watch_parser = sub.add_parser("watch", help="Scanner wiederholt ausführen")
    watch_parser.add_argument("--interval", type=int, default=None)
    watch_parser.add_argument("--runs", type=int, default=0, help="0 = unbegrenzt")

    label_parser = sub.add_parser("label", help="Abgeschlossene Lernereignisse exportieren")
    label_parser.add_argument("--hours", type=float, default=24.0)
    label_parser.add_argument("--threshold", type=float, default=0.20)
    label_parser.add_argument("--output", default="output/training_data.csv")

    train_parser = sub.add_parser("train", help="Wahrscheinlichkeitsmodell manuell trainieren")
    train_parser.add_argument("--hours", type=float, default=24.0)
    train_parser.add_argument("--threshold", type=float, default=0.20)

    learn_parser = sub.add_parser(
        "learn", help="Fällige 24-Stunden-Ergebnisse prüfen und Modelle aktualisieren"
    )
    learn_parser.add_argument("--force", action="store_true", help="Training sofort prüfen")

    sub.add_parser("dashboard", help="Startbefehl für Streamlit anzeigen")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env(args.env)
    if args.mode:
        settings.mode = args.mode
    if getattr(args, "top", None):
        settings.top_n = args.top
    if getattr(args, "universe", None):
        settings.universe_size = args.universe
    settings.ensure_directories()
    storage = Storage(settings.database_path)

    if args.command == "scan":
        result = MomentumScanner(settings, _provider(settings), storage).scan_once()
        _print_result(result)
        return

    if args.command == "watch":
        interval = args.interval or settings.scan_interval_seconds
        scanner = MomentumScanner(settings, _provider(settings), storage)
        completed = 0
        while args.runs == 0 or completed < args.runs:
            _print_result(scanner.scan_once())
            completed += 1
            if args.runs and completed >= args.runs:
                break
            time.sleep(interval)
        return

    if args.command == "label":
        frame = build_training_frame(storage, args.hours, args.threshold)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(output, index=False)
        positives = int(frame["label"].sum()) if not frame.empty else 0
        print(f"{len(frame)} gelabelte Zeilen, {positives} positive Fälle -> {output}")
        return

    if args.command == "train":
        metrics = train_probability_model(
            storage, settings.model_path, args.hours, args.threshold
        )
        print(json.dumps(metrics, indent=2, ensure_ascii=False))
        print(f"Modellziel gespeichert: {metrics.get('model_path')}")
        return

    if args.command == "learn":
        scanner = MomentumScanner(settings, _provider(settings), storage)
        summary = scanner.run_learning_cycle(force_training=args.force)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    if args.command == "dashboard":
        print("Starten mit:")
        print("streamlit run src/spike_scanner/dashboard.py")


if __name__ == "__main__":
    main()
