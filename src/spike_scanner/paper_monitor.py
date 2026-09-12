from __future__ import annotations

import csv
import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from spike_scanner.config import Settings
from spike_scanner.paper_trading import PaperTradingConfig, PaperTradingStore
from spike_scanner.ui_theme import COLORS, add_branded_header, apply_dark_theme, make_metric_card

APP_VERSION = "0.5.0"

POSITION_LABELS = {
    "status": "Status",
    "symbol": "Symbol",
    "mode": "Modus",
    "strategy_profile": "Profil",
    "phase": "Phase",
    "opened_at": "Eröffnet (UTC)",
    "entry_fill_price": "Einstieg",
    "stop_price": "Stop",
    "target_price": "Ziel",
    "last_price": "Letzter Kurs",
    "max_favorable_pct": "Max. + %",
    "max_adverse_pct": "Max. − %",
    "mover_change_pct": "Mover %",
    "mover_relative_volume": "Mover-RVOL",
    "scan_reference_price": "Scan-Kurs",
    "refreshed_entry_price": "Frisch-Kurs",
    "entry_price_drift_pct": "Drift %",
    "entry_quote_age_seconds": "Quote-Alter s",
    "entry_guard_result": "Entry-Guard",
    "entry_guard_spread_pct": "Frisch-Spread",
    "exit_reason": "Ausstieg",
    "net_return_pct": "Netto %",
}

DECISION_LABELS = {
    "created_at": "Zeit (UTC)",
    "symbol": "Symbol",
    "decision": "Entscheidung",
    "phase": "Phase",
    "price": "Kurs",
    "signal_score": "Signal-Score",
    "risk_score": "Risiko-Score",
    "purchase_score": "Kauf-Score",
    "model_probability": "Modell-Wahrsch.",
    "strategy_profile": "Profil",
    "mover_change_pct": "Mover %",
    "mover_relative_volume": "Mover-RVOL",
    "refreshed_entry_price": "Frisch-Kurs",
    "entry_price_drift_pct": "Drift %",
    "entry_quote_age_seconds": "Quote-Alter s",
    "entry_guard_result": "Entry-Guard",
    "entry_guard_spread_pct": "Frisch-Spread",
    "reason": "Begründung",
}


class PaperMonitor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Papertrading-Monitor {APP_VERSION}")
        self.geometry("1500x850")
        self.minsize(1150, 680)
        apply_dark_theme(self)

        settings = Settings.from_env(".env")
        self.config_data = PaperTradingConfig.from_env(settings)
        self.store = PaperTradingStore(self.config_data.database_path)

        self.mode_value = tk.StringVar(value="–")
        self.profile_value = tk.StringVar(value="–")
        self.open_value = tk.StringVar(value="0")
        self.closed_value = tk.StringVar(value="0")
        self.win_rate_value = tk.StringVar(value="0,0 %")
        self.avg_return_value = tk.StringVar(value="0,00 %")
        self.status_var = tk.StringVar(value="Bereit.")
        self.last_update_var = tk.StringVar(value="Letzte Aktualisierung: –")

        self._build()
        self.refresh()
        self.after(10_000, self._tick)

    def _build(self) -> None:
        add_branded_header(
            self,
            title="Papertrading-Monitor",
            subtitle=(
                "Interner Forward-Test mit getrennten Positionen und Entscheidungen. "
                "Diese Oberfläche sendet keine Brokerorders."
            ),
            version=APP_VERSION,
            safety_text="INTERNES PAPERTRADING · KEINE BROKERORDERS",
        )

        toolbar = ttk.LabelFrame(self, text="STEUERUNG UND DATEN", padding=8)
        toolbar.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(toolbar, text="STATUS AKTUALISIEREN", command=self.refresh, style="Accent.TButton").pack(
            side="left", padx=(0, 7)
        )
        ttk.Button(toolbar, text="CSV EXPORTIEREN", command=self.export_csv).pack(side="left", padx=7)
        ttk.Button(toolbar, text="DATENORDNER ÖFFNEN", command=self.open_data_folder).pack(side="left", padx=7)
        ttk.Label(
            toolbar,
            text="Moduswechsel erfolgen sicher über die Trading-Schaltzentrale.",
            style="Muted.TLabel",
        ).pack(side="right", padx=8)

        metrics = tk.Frame(self, bg=COLORS["bg"])
        metrics.pack(fill="x", padx=10, pady=(0, 8))
        cards = [
            ("Paper-Modus", self.mode_value, COLORS["gold"]),
            ("Strategieprofil", self.profile_value, COLORS["gold"]),
            ("Offene Positionen", self.open_value, COLORS["blue"]),
            ("Geschlossene Trades", self.closed_value, COLORS["text"]),
            ("Trefferquote", self.win_rate_value, COLORS["green"]),
            ("Ø Netto pro Trade", self.avg_return_value, COLORS["green"]),
        ]
        for index, (title, var, color) in enumerate(cards):
            card = make_metric_card(metrics, title, var, value_color=color)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0))
            metrics.columnconfigure(index, weight=1)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        self.positions_tree = self._make_tree(
            notebook,
            [
                ("status", 80), ("symbol", 75), ("mode", 75),
                ("strategy_profile", 125), ("phase", 65), ("opened_at", 165),
                ("scan_reference_price", 90), ("refreshed_entry_price", 90),
                ("entry_price_drift_pct", 80), ("entry_guard_result", 105),
                ("entry_quote_age_seconds", 90), ("entry_guard_spread_pct", 95),
                ("entry_fill_price", 95), ("stop_price", 90), ("target_price", 90),
                ("last_price", 90), ("mover_change_pct", 85),
                ("mover_relative_volume", 90), ("max_favorable_pct", 90),
                ("max_adverse_pct", 90), ("exit_reason", 105), ("net_return_pct", 90),
            ],
            POSITION_LABELS,
        )
        notebook.add(self.positions_tree.master, text="POSITIONEN")

        self.decisions_tree = self._make_tree(
            notebook,
            [
                ("created_at", 165), ("symbol", 75), ("decision", 95),
                ("strategy_profile", 125), ("phase", 65), ("price", 85),
                ("refreshed_entry_price", 90), ("entry_price_drift_pct", 80),
                ("entry_guard_result", 105), ("entry_quote_age_seconds", 90),
                ("entry_guard_spread_pct", 95),
                ("mover_change_pct", 85), ("mover_relative_volume", 90),
                ("signal_score", 90), ("risk_score", 90),
                ("purchase_score", 95), ("model_probability", 110), ("reason", 520),
            ],
            DECISION_LABELS,
        )
        notebook.add(self.decisions_tree.master, text="ENTSCHEIDUNGEN")

        footer = tk.Frame(self, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1)
        footer.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=12, pady=10)
        tk.Label(
            footer,
            textvariable=self.last_update_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side="right", padx=12, pady=10)

    def _make_tree(self, parent, columns, labels):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[name for name, _ in columns], show="headings")
        for name, width in columns:
            tree.heading(name, text=labels.get(name, name.replace("_", " ").title()))
            tree.column(name, width=width, anchor="center" if width < 120 else "w")
        tree.tag_configure("open", foreground=COLORS["blue"])
        tree.tag_configure("win", foreground=COLORS["green"])
        tree.tag_configure("loss", foreground=COLORS["red"])
        tree.tag_configure("rejected", foreground=COLORS["orange"])
        tree.tag_configure("neutral", foreground=COLORS["text"])

        ybar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _tick(self) -> None:
        self.refresh()
        self.after(10_000, self._tick)

    @staticmethod
    def _fmt(name: str, value):
        if value is None:
            return "—"
        if name in {
            "entry_fill_price", "stop_price", "target_price", "last_price", "price",
            "scan_reference_price", "refreshed_entry_price"
        }:
            return f"{float(value):.4f}"
        if name in {
            "max_favorable_pct", "max_adverse_pct", "net_return_pct",
            "mover_change_pct", "signal_score", "risk_score", "purchase_score",
            "entry_price_drift_pct", "entry_quote_age_seconds",
        }:
            return f"{float(value):.2f}"
        if name == "mover_relative_volume":
            return "—" if value is None else f"{float(value):.2f}×"
        if name == "entry_guard_spread_pct":
            return "—" if value is None else f"{float(value) * 100.0:.2f} %"
        if name == "strategy_profile":
            return {
                "top_mover_pilot": "TOP-MOVER",
                "standard": "STANDARD",
            }.get(str(value).lower(), str(value).upper())
        if name == "model_probability":
            return "—" if value is None else f"{float(value) * 100:.1f} %"
        return str(value)

    def refresh(self) -> None:
        try:
            summary = self.store.summary()
            self.config_data = PaperTradingConfig.from_env(Settings.from_env(".env"))
            mode = self.config_data.mode.upper()
            self.mode_value.set(mode)
            self.profile_value.set(
                "TOP-MOVER PILOT"
                if self.config_data.profile == "top_mover_pilot"
                else "STANDARD"
            )
            self.open_value.set(
                f"{summary['open']} / {self.config_data.max_open_positions}"
            )
            self.closed_value.set(str(summary["closed"]))
            self.win_rate_value.set(f"{summary['win_rate']:.1f} %")
            self.avg_return_value.set(f"{summary['avg_return_pct']:+.2f} %")
            open_by_phase = summary.get("open_by_phase", {})
            self.status_var.set(
                f"Profil: {self.config_data.profile} · "
                f"Offen PRE {open_by_phase.get('PRE', 0)}/{self.config_data.max_open_positions_pre}, "
                f"RTH {open_by_phase.get('RTH', 0)}/{self.config_data.max_open_positions_rth}, "
                f"ATH {open_by_phase.get('ATH', 0)}/{self.config_data.max_open_positions_ath} · "
                f"Tag max. {self.config_data.max_new_trades_per_day} · "
                f"Siege: {summary['wins']} · Verluste: {summary['losses']}"
            )
            self.last_update_var.set(f"Letzte Aktualisierung: {datetime.now():%d.%m.%Y %H:%M:%S}")
            self._fill(self.positions_tree, self.store.recent_positions(300), kind="positions")
            self._fill(self.decisions_tree, self.store.recent_decisions(500), kind="decisions")
        except Exception as exc:
            self.status_var.set(f"Aktualisierung fehlgeschlagen: {exc}")

    def _fill(self, tree: ttk.Treeview, rows, *, kind: str) -> None:
        tree.delete(*tree.get_children())
        columns = list(tree["columns"])
        for row in rows:
            tag = "neutral"
            if kind == "positions":
                if str(row.get("status") or "").upper() == "OPEN":
                    tag = "open"
                else:
                    result = row.get("net_return_pct")
                    if result is not None:
                        tag = "win" if float(result) > 0 else "loss"
            elif str(row.get("decision") or "").upper() == "REJECTED":
                tag = "rejected"
            elif str(row.get("decision") or "").upper() == "OPENED":
                tag = "win"

            tree.insert(
                "",
                "end",
                tags=(tag,),
                values=[self._fmt(name, row.get(name)) for name in columns],
            )

    def export_csv(self) -> None:
        target = filedialog.asksaveasfilename(
            title="Papertrades exportieren",
            defaultextension=".csv",
            initialfile=f"papertrades_{datetime.now():%Y%m%d_%H%M}.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not target:
            return
        rows = self.store.recent_positions(100_000)
        if not rows:
            messagebox.showinfo("Export", "Noch keine Papertrades vorhanden.")
            return
        with open(target, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        messagebox.showinfo("Export", f"Gespeichert:\n{target}")

    def open_data_folder(self) -> None:
        folder = self.store.path.resolve().parent
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])


def main() -> None:
    PaperMonitor().mainloop()


if __name__ == "__main__":
    main()
