from __future__ import annotations

import argparse
import json
from pathlib import Path

from spike_scanner.config import Settings
from spike_scanner.top_mover_learning import (
    MODULE_VERSION,
    TopMoverLearningConfig,
    TopMoverLearningStore,
)


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f} %"


def main() -> None:
    parser = argparse.ArgumentParser(description="Status des Top-Mover-Lernmoduls 2.0")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    settings = Settings.from_env(".env")
    config = TopMoverLearningConfig.from_env(settings)
    store = TopMoverLearningStore(config.database_path)
    summary = store.summary()
    recent = store.recent_cases(max(1, args.limit))

    export_path = None
    if args.export:
        export_path = store.export_csv(Path("output/top_mover_learning_cases.csv"))

    payload = {
        "enabled": config.enabled,
        "database": str(store.path.resolve()),
        "horizon_hours": config.horizon_hours,
        "top_movers_per_scan": config.top_movers_per_scan,
        "controls_per_scan": config.controls_per_scan,
        "min_change": config.min_change_ratio,
        "min_relative_volume": config.min_relative_volume,
        "stop_pct": config.stop_pct,
        "targets": config.targets,
        "summary": summary,
        "recent": recent,
        "export": str(export_path.resolve()) if export_path else None,
    }

    if args.as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return

    print(f"TOP-MOVER-LERNMODUL {MODULE_VERSION} – DATENSAMMLUNG")
    print("Datenbank:", store.path.resolve())
    print("Aktiv:", "JA" if config.enabled else "NEIN")
    print(
        "Auswahl:",
        f"Top {config.top_movers_per_scan} ab {_pct(config.min_change_ratio)}",
        f"und RVOL {config.min_relative_volume:.1f}",
        f"plus {config.controls_per_scan} Kontrollen",
    )
    print(
        "Auswertung:",
        f"{config.horizon_hours:g} Stunden, Stop {_pct(config.stop_pct)},",
        "Ziele " + ", ".join(_pct(item) for item in config.targets),
    )
    print()

    print("Fälle nach Status:")
    status = summary.get("status") or {}
    if not status:
        print("  noch keine Fälle")
    for cohort, values in status.items():
        print(
            f"  {cohort}: pending={values.get('pending', 0)} | "
            f"finalized={values.get('finalized', 0)}"
        )

    print("\nZiel-vor-Stop-Ergebnisse:")
    targets = summary.get("targets") or {}
    if not targets:
        print("  noch keine ausgewerteten Fälle")
    for key, values in sorted(targets.items(), key=lambda item: float(item[0])):
        print(
            f"  +{float(key) * 100:.0f} %: "
            f"Ziel zuerst={values.get('target_before_stop', 0)} | "
            f"Stop zuerst={values.get('stop_before_target', 0)} | "
            f"mehrdeutig={values.get('ambiguous', 0)} | "
            f"kein Ereignis={values.get('no_event', 0)}"
        )

    print("\nLetzte Fälle:")
    if not recent:
        print("  noch keine Fälle")
    for row in recent:
        print(
            f"  {row['signal_timestamp']} | {row['symbol']:<6} | "
            f"{row['cohort']:<9} | {row['phase']:<3} | "
            f"Mover {_pct(row['change_ratio']):>8} | "
            f"RVOL {float(row['relative_volume']):>5.2f} | "
            f"{row['status']} | {row.get('outcome') or '—'}"
        )

    if export_path:
        print("\nCSV exportiert:", export_path.resolve())


if __name__ == "__main__":
    main()
