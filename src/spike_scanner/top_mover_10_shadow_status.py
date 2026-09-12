from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from spike_scanner.top_mover_10_shadow import TopMover10ShadowConfig


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _count(con: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    row = con.execute(query, params).fetchone()
    return int(row[0] if row else 0)


def build_status(path: Path) -> dict[str, Any]:
    config = TopMover10ShadowConfig.from_env()
    if not path.exists():
        return {
            "enabled": config.enabled,
            "database": str(path.resolve()),
            "exists": False,
            "open": 0,
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "time_exits": 0,
            "ambiguous": 0,
            "observed_ath": 0,
            "observed_nonselected": 0,
            "configuration": _config_summary(config),
        }

    with _connect(path) as con:
        open_count = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'")
        closed_count = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE status='CLOSED'")
        wins = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE exit_reason='TARGET'")
        losses = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE exit_reason='STOP'")
        time_exits = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE exit_reason='TIME'")
        ambiguous = _count(
            con,
            "SELECT COUNT(*) FROM paper_positions WHERE exit_reason='AMBIGUOUS'",
        )
        observed_ath = _count(
            con,
            """
            SELECT COUNT(*) FROM paper_decisions
            WHERE decision='OBSERVED' AND UPPER(phase)='ATH'
            """,
        )
        observed_nonselected = _count(
            con,
            """
            SELECT COUNT(*) FROM paper_decisions
            WHERE decision='OBSERVED' AND UPPER(phase) IN ('PRE','RTH')
            """,
        )
        open_by_phase = {
            row["phase"]: int(row["n"])
            for row in con.execute(
                """
                SELECT UPPER(phase) AS phase, COUNT(*) AS n
                FROM paper_positions WHERE status='OPEN'
                GROUP BY UPPER(phase)
                """
            ).fetchall()
        }
        recent_positions = [
            dict(row)
            for row in con.execute(
                """
                SELECT symbol, phase, status, opened_at, entry_fill_price,
                       stop_price, target_price, purchase_score, exit_reason,
                       net_return_pct
                FROM paper_positions ORDER BY opened_at DESC LIMIT 20
                """
            ).fetchall()
        ]

    denominator = wins + losses
    return {
        "enabled": config.enabled,
        "database": str(path.resolve()),
        "exists": True,
        "open": open_count,
        "open_by_phase": open_by_phase,
        "closed": closed_count,
        "wins": wins,
        "losses": losses,
        "time_exits": time_exits,
        "ambiguous": ambiguous,
        "target_before_stop_rate_pct": round(wins / denominator * 100.0, 2)
        if denominator
        else 0.0,
        "observed_ath": observed_ath,
        "observed_nonselected": observed_nonselected,
        "configuration": _config_summary(config),
        "recent_positions": recent_positions,
    }


def _config_summary(config: TopMover10ShadowConfig) -> dict[str, Any]:
    return {
        "profile": "top_mover_10_shadow",
        "mode": "shadow_only",
        "target_pct": config.target_pct,
        "stop_loss_pct": config.stop_loss_pct,
        "max_hold_hours": config.max_hold_hours,
        "max_open_positions": config.max_open_positions,
        "max_new_per_day": config.max_new_trades_per_day,
        "PRE": {
            "mode": "active_test",
            "max_open": config.max_open_pre,
            "max_rank": config.pre_max_rank,
        },
        "RTH": {
            "mode": "active_core",
            "max_open": config.max_open_rth,
            "max_rank": config.rth_max_rank,
        },
        "ATH": {"mode": "observe_only", "max_open": 0},
        "entry_guard": config.entry_guard_enabled,
        "cooldown_hours": config.cooldown_hours,
    }


def export_positions(path: Path, destination: Path) -> int:
    if not path.exists():
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as con:
        rows = con.execute(
            "SELECT * FROM paper_positions ORDER BY opened_at, id"
        ).fetchall()
        columns = [item[1] for item in con.execute("PRAGMA table_info(paper_positions)")]
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return len(rows)


def main() -> int:
    try:
        from spike_scanner.config import Settings
        Settings.from_env(".env")
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()

    config = TopMover10ShadowConfig.from_env()
    path = config.database_path
    status = build_status(path)
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))

    if args.export:
        output = Path("output/top_mover_10_shadow_positions.csv")
        count = export_positions(path, output)
        print(f"\nEXPORT_OK: {output.resolve()} ({count} Positionen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
