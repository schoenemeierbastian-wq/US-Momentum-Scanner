from __future__ import annotations

import argparse
import html
import json
import sqlite3
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "data" / "papertrades.sqlite"
STATE_FILE = ROOT / "data" / "paper_report_state.json"
REPORT_DIR = ROOT / "reports"
REPORT_FILE = REPORT_DIR / "papertrading_report.html"

REPORT_INTERVAL_DAYS = 7


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def local_text(value: str | None) -> str:
    dt = parse_dt(value)
    if dt is None:
        return "-"
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return result

    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def create_test_state() -> dict[str, Any]:
    env = read_env()
    now = utc_now()

    state = {
        "test_name": "Qualitaetsfilter 2026-09-06",
        "test_start_utc": now.isoformat(),
        "report_interval_days": REPORT_INTERVAL_DAYS,
        "last_report_at_utc": None,
        "rules": {
            "breakdown_max_pct": env.get(
                "PAPER_ENTRY_GUARD_MAX_BREAKDOWN_PCT", "0.015"
            ),
            "chase_max_pct": env.get(
                "PAPER_ENTRY_GUARD_MAX_CHASE_PCT", "0.03"
            ),
            "signal_score_max": "60",
        },
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return state


def load_state(new_test: bool = False) -> dict[str, Any]:
    if new_test or not STATE_FILE.exists():
        return create_test_state()

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return create_test_state()


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def avg(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [num(row.get(field)) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:+.{digits}f} %"


def fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    opened = [r for r in rows if str(r.get("status", "")).upper() == "OPEN"]
    closed = [r for r in rows if str(r.get("status", "")).upper() == "CLOSED"]

    wins = [r for r in closed if (num(r.get("net_return_pct")) or 0) > 0]
    losses = [r for r in closed if (num(r.get("net_return_pct")) or 0) <= 0]

    return {
        "open": len(opened),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed) * 100.0) if closed else None,
        "avg_net": avg(closed, "net_return_pct"),
        "avg_win": avg(wins, "net_return_pct"),
        "avg_loss": avg(losses, "net_return_pct"),
        "wins_rows": wins,
        "loss_rows": losses,
        "closed_rows": closed,
    }


def after_test_start(
    row: dict[str, Any],
    start: datetime,
    field: str = "opened_at",
) -> bool:
    dt = parse_dt(row.get(field))
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= start


def metric_card(title: str, value: str, sub: str = "") -> str:
    return f"""
    <div class="card">
      <div class="card-title">{html.escape(title)}</div>
      <div class="card-value">{html.escape(value)}</div>
      <div class="card-sub">{html.escape(sub)}</div>
    </div>
    """


def compare_table(closed: list[dict[str, Any]]) -> str:
    wins = [r for r in closed if (num(r.get("net_return_pct")) or 0) > 0]
    losses = [r for r in closed if (num(r.get("net_return_pct")) or 0) <= 0]

    fields = [
        ("Signal-Score", "signal_score"),
        ("Purchase-Score", "purchase_score"),
        ("Mover %", "mover_change_pct"),
        ("RVOL", "mover_relative_volume"),
        ("Entry-Drift %", "entry_price_drift_pct"),
        ("Entry-Spread", "entry_guard_spread_pct"),
    ]

    lines = []
    for label, field in fields:
        lines.append(
            "<tr>"
            f"<td>{html.escape(label)}</td>"
            f"<td>{fmt_num(avg(wins, field))}</td>"
            f"<td>{fmt_num(avg(losses, field))}</td>"
            "</tr>"
        )

    return "\n".join(lines)


def trade_rows(rows: list[dict[str, Any]], limit: int = 30) -> str:
    ordered = sorted(
        rows,
        key=lambda r: str(r.get("opened_at") or ""),
        reverse=True,
    )[:limit]

    output = []
    for row in ordered:
        output.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('symbol') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('phase') or '-'))}</td>"
            f"<td>{local_text(row.get('opened_at'))}</td>"
            f"<td>{local_text(row.get('closed_at'))}</td>"
            f"<td>{fmt_pct(num(row.get('net_return_pct')))}</td>"
            f"<td>{fmt_num(num(row.get('signal_score')), 1)}</td>"
            f"<td>{fmt_num(num(row.get('purchase_score')), 1)}</td>"
            f"<td>{fmt_num(num(row.get('mover_change_pct')), 1)}</td>"
            f"<td>{fmt_num(num(row.get('mover_relative_volume')), 1)}</td>"
            f"<td>{fmt_num(num(row.get('entry_price_drift_pct')), 2)}</td>"
            f"<td>{html.escape(str(row.get('entry_guard_result') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('exit_reason') or '-'))}</td>"
            "</tr>"
        )
    return "\n".join(output)


def rejection_rows(decisions: list[dict[str, Any]]) -> str:
    counter: Counter[str] = Counter()

    for row in decisions:
        if str(row.get("decision", "")).upper() != "REJECTED":
            continue

        reason = str(row.get("reason") or "Unbekannt")
        for part in reason.split(" | "):
            text = part.strip()
            if text:
                counter[text] += 1

    return "\n".join(
        f"<tr><td>{count}</td><td>{html.escape(reason)}</td></tr>"
        for reason, count in counter.most_common(20)
    )


def grouped_table(rows: list[dict[str, Any]], field: str) -> str:
    groups: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        key = str(row.get(field) or "UNBEKANNT")
        groups.setdefault(key, []).append(row)

    output = []
    for key, group in sorted(groups.items()):
        m = metrics(group)
        output.append(
            "<tr>"
            f"<td>{html.escape(key)}</td>"
            f"<td>{m['closed']}</td>"
            f"<td>{m['wins']}</td>"
            f"<td>{fmt_num(m['win_rate'], 1)} %</td>"
            f"<td>{fmt_pct(m['avg_net'])}</td>"
            "</tr>"
        )

    return "\n".join(output)


def generate_report(new_test: bool = False, open_browser: bool = True) -> Path:
    state = load_state(new_test=new_test)

    start = parse_dt(state["test_start_utc"])
    if start is None:
        start = utc_now()
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    if not DATABASE.exists():
        raise SystemExit(f"Paper-Datenbank nicht gefunden: {DATABASE}")

    con = sqlite3.connect(DATABASE)
    con.row_factory = sqlite3.Row

    positions = [
        dict(row)
        for row in con.execute(
            "SELECT * FROM paper_positions ORDER BY opened_at"
        ).fetchall()
    ]

    decisions = [
        dict(row)
        for row in con.execute(
            "SELECT * FROM paper_decisions ORDER BY created_at"
        ).fetchall()
    ]

    con.close()

    test_positions = [
        row for row in positions
        if after_test_start(row, start, "opened_at")
    ]

    test_decisions = [
        row for row in decisions
        if after_test_start(row, start, "created_at")
    ]

    overall = metrics(positions)
    test = metrics(test_positions)

    exit_counter = Counter(
        str(r.get("exit_reason") or "UNBEKANNT")
        for r in test["closed_rows"]
    )

    exit_rows = "\n".join(
        f"<tr><td>{html.escape(reason)}</td><td>{count}</td></tr>"
        for reason, count in exit_counter.most_common()
    )

    rules = state.get("rules", {})
    breakdown = float(rules.get("breakdown_max_pct", 0.015)) * 100.0
    chase = float(rules.get("chase_max_pct", 0.03)) * 100.0
    signal_max = rules.get("signal_score_max", "60")

    generated = utc_now()
    start_local = start.astimezone().strftime("%d.%m.%Y %H:%M")
    generated_local = generated.astimezone().strftime("%d.%m.%Y %H:%M")

    if test["closed"] >= 10:
        if overall["avg_net"] is not None and test["avg_net"] is not None:
            if test["avg_net"] > overall["avg_net"] + 0.5:
                verdict = "Aktueller Teststand verbessert sich"
                verdict_class = "good"
            elif test["avg_net"] < overall["avg_net"] - 0.5:
                verdict = "Aktueller Teststand ist schwächer"
                verdict_class = "bad"
            else:
                verdict = "Noch keine klare Verbesserung"
                verdict_class = "neutral"
        else:
            verdict = "Auswertung möglich"
            verdict_class = "neutral"
    else:
        verdict = (
            f"Noch keine belastbare Bewertung – "
            f"{test['closed']}/10 neue geschlossene Trades"
        )
        verdict_class = "neutral"

    css = """
    body {
        background:#0e1a2b;
        color:#e8eef7;
        font-family:Segoe UI,Arial,sans-serif;
        margin:0;
        padding:28px;
    }
    h1,h2 { color:#f4b321; }
    .sub { color:#9fb0c5; margin-bottom:24px; }
    .grid {
        display:grid;
        grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
        gap:12px;
        margin:18px 0 28px;
    }
    .card {
        background:#14243a;
        border:1px solid #344a66;
        padding:16px;
    }
    .card-title {
        color:#aebed1;
        font-size:13px;
        text-transform:uppercase;
    }
    .card-value {
        font-size:27px;
        margin-top:6px;
    }
    .card-sub {
        color:#8295ac;
        font-size:12px;
        margin-top:4px;
    }
    .rule {
        display:inline-block;
        padding:8px 12px;
        margin:4px;
        background:#23364f;
        border:1px solid #405876;
    }
    table {
        width:100%;
        border-collapse:collapse;
        margin-bottom:28px;
        background:#14243a;
    }
    th,td {
        border-bottom:1px solid #30455f;
        padding:9px;
        text-align:left;
        font-size:13px;
    }
    th { color:#f4b321; }
    .good { color:#35e176; }
    .bad { color:#ff5b5b; }
    .neutral { color:#f4b321; }
    .section {
        margin-top:32px;
    }
    """

    page = f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Papertrading-Bericht</title>
<style>{css}</style>
</head>
<body>

<h1>Papertrading-Bericht</h1>
<div class="sub">
Erstellt {generated_local} · Datenbank: {html.escape(str(DATABASE))}
</div>

<h2>Gesamtbestand</h2>
<div class="grid">
{metric_card("Offen", str(overall["open"]))}
{metric_card("Geschlossen", str(overall["closed"]))}
{metric_card("Trefferquote", (fmt_num(overall["win_rate"], 1) + " %") if overall["win_rate"] is not None else "-")}
{metric_card("Ø Netto pro Trade", fmt_pct(overall["avg_net"]))}
{metric_card("Ø Gewinner", fmt_pct(overall["avg_win"]))}
{metric_card("Ø Verlierer", fmt_pct(overall["avg_loss"]))}
</div>

<div class="section">
<h2>Aktueller Teststand</h2>
<p><strong>{html.escape(str(state.get("test_name", "Aktueller Test")))}</strong>
· seit {start_local}</p>

<div>
<span class="rule">Breakdown max. -{breakdown:.1f} %</span>
<span class="rule">Signal-Score max. {html.escape(str(signal_max))}</span>
<span class="rule">CHASE max. +{chase:.1f} %</span>
</div>

<div class="grid">
{metric_card("Neue offene Trades", str(test["open"]))}
{metric_card("Neue geschlossene Trades", str(test["closed"]), "Bewertung ab 10")}
{metric_card("Neue Gewinner", str(test["wins"]))}
{metric_card("Neue Verlierer", str(test["losses"]))}
{metric_card("Neue Trefferquote", (fmt_num(test["win_rate"], 1) + " %") if test["win_rate"] is not None else "-")}
{metric_card("Neues Ø Netto", fmt_pct(test["avg_net"]))}
</div>

<h2 class="{verdict_class}">{html.escape(verdict)}</h2>
</div>

<div class="section">
<h2>Gewinner vs. Verlierer – aktueller Test</h2>
<table>
<tr><th>Merkmal</th><th>Gewinner Ø</th><th>Verlierer Ø</th></tr>
{compare_table(test["closed_rows"])}
</table>
</div>

<div class="section">
<h2>Ergebnis nach Marktphase</h2>
<table>
<tr><th>Phase</th><th>Closed</th><th>Gewinner</th><th>Trefferquote</th><th>Ø Netto</th></tr>
{grouped_table(test["closed_rows"], "phase")}
</table>
</div>

<div class="section">
<h2>Exit-Gründe</h2>
<table>
<tr><th>Grund</th><th>Anzahl</th></tr>
{exit_rows or '<tr><td colspan="2">Noch keine geschlossenen Test-Trades</td></tr>'}
</table>
</div>

<div class="section">
<h2>Ablehnungen seit Teststart</h2>
<table>
<tr><th>Anzahl</th><th>Ablehnungsgrund</th></tr>
{rejection_rows(test_decisions) or '<tr><td colspan="2">Noch keine Ablehnungen im Testzeitraum</td></tr>'}
</table>
</div>

<div class="section">
<h2>Trades seit Teststart</h2>
<table>
<tr>
<th>Symbol</th>
<th>Phase</th>
<th>Eröffnet</th>
<th>Geschlossen</th>
<th>Netto</th>
<th>Signal</th>
<th>Purchase</th>
<th>Move %</th>
<th>RVOL</th>
<th>Drift %</th>
<th>Guard</th>
<th>Exit</th>
</tr>
{trade_rows(test_positions) or '<tr><td colspan="12">Noch keine neuen Test-Trades</td></tr>'}
</table>
</div>

</body>
</html>
"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(page, encoding="utf-8")

    state["last_report_at_utc"] = generated.isoformat()
    save_state(state)

    print("PAPERTRADING-BERICHT OK")
    print("Datei:", REPORT_FILE)
    print("Teststart:", start_local)
    print("Gesamt geschlossen:", overall["closed"])
    print("Seit Teststart geschlossen:", test["closed"])
    print("Seit Teststart offen:", test["open"])

    if open_browser:
        webbrowser.open(REPORT_FILE.resolve().as_uri())

    return REPORT_FILE


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--new-test",
        action="store_true",
        help="Neuen Testzeitraum ab jetzt beginnen.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Bericht nicht automatisch im Browser öffnen.",
    )
    args = parser.parse_args()

    generate_report(
        new_test=args.new_test,
        open_browser=not args.no_open,
    )


if __name__ == "__main__":
    main()
