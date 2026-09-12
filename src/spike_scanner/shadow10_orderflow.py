from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fmt_duration(start: Any, end: Any = None) -> str:
    opened = _parse_datetime(start)
    closed = _parse_datetime(end) or datetime.now(timezone.utc)
    if opened is None:
        return "–"
    seconds = max(0, int((closed - opened).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}T {hours:02d}:{minutes:02d}"
    return f"{hours:02d}:{minutes:02d}"


def _metadata(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _number(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _count(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    try:
        row = con.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] if row and row[0] is not None else 0)


@dataclass(slots=True)
class Shadow10Snapshot:
    database: str
    exists: bool
    installed: bool
    status_text: str
    rows: list[dict[str, Any]]
    open_total: int = 0
    open_rth: int = 0
    open_pre: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    time_exits: int = 0
    ambiguous: int = 0
    observed_ath: int = 0
    opened_today: int = 0
    target_before_stop_rate_pct: float | None = None
    last_activity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "exists": self.exists,
            "installed": self.installed,
            "status_text": self.status_text,
            "rows": self.rows,
            "open_total": self.open_total,
            "open_rth": self.open_rth,
            "open_pre": self.open_pre,
            "closed": self.closed,
            "wins": self.wins,
            "losses": self.losses,
            "time_exits": self.time_exits,
            "ambiguous": self.ambiguous,
            "observed_ath": self.observed_ath,
            "opened_today": self.opened_today,
            "target_before_stop_rate_pct": self.target_before_stop_rate_pct,
            "last_activity": self.last_activity,
        }


class Shadow10Reader:
    """Read-only reader for the separate Top-Mover 10% shadow database."""

    def __init__(
        self,
        project_root: Path | str,
        database_path: Path | str | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.env = _read_env(self.project_root / ".env")
        configured = database_path or self.env.get(
            "TOP_MOVER_10_SHADOW_DATABASE_PATH",
            "data/top_mover_10_shadow.sqlite",
        )
        path = Path(configured)
        if not path.is_absolute():
            path = self.project_root / path
        self.database_path = path.resolve()
        self.module_path = (
            self.project_root / "src" / "spike_scanner" / "top_mover_10_shadow.py"
        )

    def _connect(self) -> sqlite3.Connection:
        uri = self.database_path.as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=2000")
        con.execute("PRAGMA query_only=ON")
        return con

    def snapshot(self, filter_mode: str = "OFFEN", limit: int = 500) -> Shadow10Snapshot:
        installed = self.module_path.exists()
        if not self.database_path.exists():
            text = (
                "10%-Shadow-Modul nicht installiert"
                if not installed
                else "Noch keine Shadow-Datenbank – sie entsteht nach dem ersten vollständigen Scan"
            )
            return Shadow10Snapshot(
                database=str(self.database_path),
                exists=False,
                installed=installed,
                status_text=text,
                rows=[],
            )

        try:
            con = self._connect()
        except sqlite3.Error as exc:
            return Shadow10Snapshot(
                database=str(self.database_path),
                exists=True,
                installed=installed,
                status_text=f"Datenbank vorübergehend nicht lesbar: {exc}",
                rows=[],
            )

        try:
            if not _table_exists(con, "paper_positions"):
                return Shadow10Snapshot(
                    database=str(self.database_path),
                    exists=True,
                    installed=installed,
                    status_text="Datenbank vorhanden, Tabelle paper_positions fehlt noch",
                    rows=[],
                )

            mode = str(filter_mode or "OFFEN").upper()
            where = ""
            if mode == "OFFEN":
                where = "WHERE UPPER(status)='OPEN'"
            elif mode == "GESCHLOSSEN":
                where = "WHERE UPPER(status)='CLOSED'"

            raw_rows = con.execute(
                f"""
                SELECT * FROM paper_positions
                {where}
                ORDER BY CASE WHEN UPPER(status)='OPEN' THEN 0 ELSE 1 END,
                         COALESCE(opened_at, '') DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            rows = [self._normalize_row(dict(row)) for row in raw_rows]

            open_total = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE UPPER(status)='OPEN'")
            open_rth = _count(
                con,
                "SELECT COUNT(*) FROM paper_positions WHERE UPPER(status)='OPEN' AND UPPER(phase)='RTH'",
            )
            open_pre = _count(
                con,
                "SELECT COUNT(*) FROM paper_positions WHERE UPPER(status)='OPEN' AND UPPER(phase)='PRE'",
            )
            closed = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE UPPER(status)='CLOSED'")
            wins = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE UPPER(exit_reason)='TARGET'")
            losses = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE UPPER(exit_reason)='STOP'")
            time_exits = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE UPPER(exit_reason)='TIME'")
            ambiguous = _count(con, "SELECT COUNT(*) FROM paper_positions WHERE UPPER(exit_reason)='AMBIGUOUS'")
            today_et = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
            opened_today = _count(
                con,
                "SELECT COUNT(*) FROM paper_positions WHERE opened_trade_date_et=?",
                (today_et,),
            )
            observed_ath = 0
            if _table_exists(con, "paper_decisions"):
                observed_ath = _count(
                    con,
                    """
                    SELECT COUNT(*) FROM paper_decisions
                    WHERE UPPER(decision)='OBSERVED' AND UPPER(phase)='ATH'
                    """,
                )

            last_row = con.execute(
                """
                SELECT MAX(COALESCE(closed_at, last_bar_at, opened_at))
                FROM paper_positions
                """
            ).fetchone()
            last_activity = str(last_row[0]) if last_row and last_row[0] else None
            denom = wins + losses
            rate = round(wins / denom * 100.0, 2) if denom else None
            return Shadow10Snapshot(
                database=str(self.database_path),
                exists=True,
                installed=installed,
                status_text="Read-only verbunden",
                rows=rows,
                open_total=open_total,
                open_rth=open_rth,
                open_pre=open_pre,
                closed=closed,
                wins=wins,
                losses=losses,
                time_exits=time_exits,
                ambiguous=ambiguous,
                observed_ath=observed_ath,
                opened_today=opened_today,
                target_before_stop_rate_pct=rate,
                last_activity=last_activity,
            )
        except sqlite3.Error as exc:
            return Shadow10Snapshot(
                database=str(self.database_path),
                exists=True,
                installed=installed,
                status_text=f"Lesefehler: {exc}",
                rows=[],
            )
        finally:
            con.close()

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        meta = _metadata(row.get("metadata_json"))
        candidate = meta.get("candidate") if isinstance(meta.get("candidate"), dict) else {}
        entry = _number(row.get("entry_fill_price")) or _number(row.get("entry_reference_price"))
        current = (
            _number(row.get("last_price"))
            or _number(row.get("exit_fill_price"))
            or _number(row.get("exit_reference_price"))
            or entry
        )
        target = _number(row.get("target_price"))
        stop = _number(row.get("stop_price"))
        stored_return = _number(row.get("net_return_pct"))
        pnl_pct = stored_return
        if pnl_pct is None and entry and current:
            pnl_pct = (current / entry - 1.0) * 100.0
        target_distance = None
        stop_distance = None
        if current and target:
            target_distance = (target / current - 1.0) * 100.0
        if current and stop:
            stop_distance = (stop / current - 1.0) * 100.0

        opened = _parse_datetime(row.get("opened_at"))
        closed = _parse_datetime(row.get("closed_at"))
        opened_local = opened.astimezone().strftime("%d.%m. %H:%M") if opened else "–"
        closed_local = closed.astimezone().strftime("%d.%m. %H:%M") if closed else "–"

        rank = candidate.get("rank") if isinstance(candidate, dict) else None
        priority = meta.get("purchase_priority")
        exit_reason = str(row.get("exit_reason") or "")
        status = str(row.get("status") or "").upper()
        display_status = "OFFEN" if status == "OPEN" else (exit_reason or "GESCHLOSSEN")
        return {
            "id": row.get("id"),
            "symbol": str(row.get("symbol") or "").upper(),
            "phase": str(row.get("phase") or "OFF").upper(),
            "status": status,
            "display_status": display_status,
            "opened_at": row.get("opened_at"),
            "opened_local": opened_local,
            "closed_at": row.get("closed_at"),
            "closed_local": closed_local,
            "hold": _fmt_duration(row.get("opened_at"), row.get("closed_at")),
            "entry": entry,
            "current": current,
            "pnl_pct": pnl_pct,
            "target": target,
            "stop": stop,
            "target_distance_pct": target_distance,
            "stop_distance_pct": stop_distance,
            "rank": rank,
            "priority": priority,
            "purchase_score": _number(row.get("purchase_score")),
            "signal_score": _number(row.get("signal_score")),
            "risk_score": _number(row.get("risk_score")),
            "rvol": _number(row.get("mover_relative_volume")),
            "mover_change_pct": _number(row.get("mover_change_pct")),
            "spread_pct": _number(row.get("entry_guard_spread_pct"))
            if row.get("entry_guard_spread_pct") is not None
            else _number(row.get("entry_spread_pct")),
            "entry_guard": str(row.get("entry_guard_result") or "–"),
            "exit_reason": exit_reason,
            "max_favorable_pct": _number(row.get("max_favorable_pct")),
            "max_adverse_pct": _number(row.get("max_adverse_pct")),
            "source_run_id": row.get("source_run_id"),
        }

    def export(self, destination: Path | str | None = None) -> tuple[Path, int]:
        path = Path(destination) if destination else (
            self.project_root / "output" / "top_mover_10_shadow_orderflow_view.csv"
        )
        if not path.is_absolute():
            path = self.project_root / path
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot(filter_mode="ALLE", limit=100000)
        fields = [
            "id", "symbol", "phase", "display_status", "opened_local", "closed_local",
            "hold", "entry", "current", "pnl_pct", "target", "stop",
            "target_distance_pct", "stop_distance_pct", "rank", "priority",
            "purchase_score", "signal_score", "risk_score", "rvol",
            "mover_change_pct", "spread_pct", "entry_guard", "exit_reason",
            "max_favorable_pct", "max_adverse_pct", "source_run_id",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(snapshot.rows)
        return path, len(snapshot.rows)


def _selftest() -> int:
    with tempfile.TemporaryDirectory(prefix="shadow10_orderflow_") as tmp:
        root = Path(tmp)
        (root / "src" / "spike_scanner").mkdir(parents=True)
        (root / "src" / "spike_scanner" / "top_mover_10_shadow.py").write_text("# test\n", encoding="utf-8")
        (root / "data").mkdir()
        db = root / "data" / "top_mover_10_shadow.sqlite"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE paper_positions(
                id INTEGER PRIMARY KEY, symbol TEXT, phase TEXT, status TEXT,
                opened_at TEXT, opened_trade_date_et TEXT, closed_at TEXT,
                entry_reference_price REAL, entry_fill_price REAL,
                entry_spread_pct REAL, stop_price REAL, target_price REAL,
                last_bar_at TEXT, last_price REAL, exit_fill_price REAL,
                exit_reference_price REAL, net_return_pct REAL,
                purchase_score REAL, signal_score REAL, risk_score REAL,
                mover_relative_volume REAL, mover_change_pct REAL,
                entry_guard_spread_pct REAL, entry_guard_result TEXT,
                exit_reason TEXT, max_favorable_pct REAL, max_adverse_pct REAL,
                source_run_id TEXT, metadata_json TEXT
            );
            CREATE TABLE paper_decisions(
                id INTEGER PRIMARY KEY, decision TEXT, phase TEXT
            );
            """
        )
        today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        metadata = json.dumps({"candidate": {"rank": 1}, "purchase_priority": "A"})
        con.executemany(
            """
            INSERT INTO paper_positions(
                id, symbol, phase, status, opened_at, opened_trade_date_et,
                closed_at, entry_fill_price, stop_price, target_price, last_price,
                net_return_pct, purchase_score, signal_score, risk_score,
                mover_relative_volume, mover_change_pct, entry_guard_spread_pct,
                entry_guard_result, exit_reason, max_favorable_pct, max_adverse_pct,
                source_run_id, metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (1,"AAA","RTH","OPEN","2026-09-01T14:00:00+00:00",today,None,2.0,1.86,2.2,2.1,None,70,80,20,4.0,25,0.01,"PASS",None,5,-2,"r1",metadata),
                (2,"BBB","PRE","CLOSED","2026-09-01T10:00:00+00:00",today,"2026-09-01T11:00:00+00:00",1.0,0.93,1.1,1.1,9.7,66,75,25,3.0,20,0.02,"PASS","TARGET",11,-1,"r2",metadata),
                (3,"CCC","RTH","CLOSED","2026-09-01T15:00:00+00:00",today,"2026-09-01T16:00:00+00:00",1.0,0.93,1.1,0.93,-7.2,60,70,30,2.5,15,0.01,"PASS","STOP",2,-8,"r3",metadata),
                (4,"DDD","RTH","CLOSED","2026-09-01T15:00:00+00:00",today,"2026-09-01T16:00:00+00:00",1.0,0.93,1.1,0.93,-7.2,60,70,30,2.5,15,0.01,"PASS","AMBIGUOUS",10,-8,"r4",metadata),
            ],
        )
        con.execute("INSERT INTO paper_decisions(decision, phase) VALUES ('OBSERVED','ATH')")
        con.commit()
        con.close()

        reader = Shadow10Reader(root, db)
        snap = reader.snapshot("ALLE")
        assert snap.exists and snap.installed
        assert snap.open_total == 1 and snap.open_rth == 1
        assert snap.wins == 1 and snap.losses == 1 and snap.ambiguous == 1
        assert snap.observed_ath == 1
        assert snap.target_before_stop_rate_pct == 50.0
        assert len(snap.rows) == 4 and snap.rows[0]["symbol"] == "AAA"
        assert snap.rows[0]["rank"] == 1
        export, count = reader.export()
        assert export.exists() and count == 4
    print("SHADOW10_ORDERFLOW_SELFTEST_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--filter", default="OFFEN")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    reader = Shadow10Reader(args.project)
    if args.export:
        path, count = reader.export()
        print(json.dumps({"export": str(path), "count": count}, ensure_ascii=False))
    else:
        print(json.dumps(reader.snapshot(args.filter).to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
