from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from datetime import datetime, timezone
import tkinter as tk
from tkinter import messagebox, ttk

from spike_scanner.config import Settings
from spike_scanner.orderflow import OrderflowMonitor, OrderflowStorage, ScannerContext
from spike_scanner.orderflow.auto_sampling import AutoSamplingState
from spike_scanner.orderflow.trend_analysis import analyse_history
from spike_scanner.providers import (
    MockMarketDataProvider,
    WebullMarketDataProvider,
    is_transient_network_error,
)
from spike_scanner.ui_theme import COLORS, add_branded_header, apply_dark_theme
from spike_scanner.shadow10_orderflow import Shadow10Reader

logger = logging.getLogger(__name__)

APP_VERSION = "0.8.0"
RETRY_DELAYS_SECONDS = (5, 10, 20)


def _provider(settings: Settings):
    if settings.mode == "mock":
        return MockMarketDataProvider()
    if settings.mode == "webull":
        settings.validate_live()
        return WebullMarketDataProvider(settings)
    raise ValueError(f"Unbekannter Modus: {settings.mode}")


def _fmt(value: float | None, decimals: int = 2, suffix: str = "") -> str:
    if value is None:
        return "–"
    return f"{value:.{decimals}f}{suffix}"


def _retry_delay(consecutive_failures: int) -> int:
    index = max(0, min(len(RETRY_DELAYS_SECONDS) - 1, consecutive_failures - 1))
    return RETRY_DELAYS_SECONDS[index]


def _measurement_error_text(row: object) -> str:
    warnings = getattr(row, "warnings", None) or []
    reason = getattr(row, "liquidity_reason", None) or ""
    return " | ".join([str(reason), *(str(item) for item in warnings)]).strip(" |")


class OrderflowMonitorApp(tk.Tk):
    """Read-only-Orderflow-Monitor ohne Einfluss auf Scanner-Signale oder Orders."""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"Orderflow-Messmodul {APP_VERSION}")
        self.geometry("1580x900")
        self.minsize(1220, 720)
        apply_dark_theme(self)

        self.settings = Settings.from_env()
        self.settings.ensure_directories()
        self.storage = OrderflowStorage(self.settings.database_path)
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()

        self.depth_var = tk.IntVar(value=1)
        self.limit_var = tk.IntVar(value=10)
        self.interval_var = tk.IntVar(value=10)
        self.symbols_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(
            value="Bereit. Spread und Liquidität sind Sicherheitsfilter; keine Kaufempfehlung."
        )
        self.auto_status_var = tk.StringVar(value="AUTOMATIK: AUS")
        self.connection_var = tk.StringVar(value="Marktdaten: bereit")
        self.last_success_var = tk.StringVar(value="Letzter Erfolg: –")
        self.use_latest_var = tk.BooleanVar(value=True)
        self.hide_errors_var = tk.BooleanVar(value=True)

        self.project_root = Path(__file__).resolve().parents[2]
        self.shadow_reader = Shadow10Reader(self.project_root)
        self.shadow_filter_var = tk.StringVar(value="OFFEN")
        self.shadow_auto_var = tk.BooleanVar(value=True)
        self.shadow_connection_var = tk.StringVar(value="10%-Shadow: wird gelesen …")
        self.shadow_open_var = tk.StringVar(value="0 / 4")
        self.shadow_phase_var = tk.StringVar(value="RTH 0/4 · PRE 0/1")
        self.shadow_today_var = tk.StringVar(value="0 / 8")
        self.shadow_result_var = tk.StringVar(value="Ziel 0 · Stop 0 · Zeit 0 · Mehrdeutig 0")
        self.shadow_rate_var = tk.StringVar(value="Ziel vor Stop: –")
        self.shadow_detail_var = tk.StringVar(value="Zeile auswählen oder doppelklicken, um das Symbol in die Orderflow-Messung zu übernehmen.")
        self._shadow_rows: dict[str, dict[str, object]] = {}
        self._shadow_job_id: str | None = None

        self.auto_state = AutoSamplingState()
        self._auto_job_id: str | None = None
        self._measurement_running = False
        self._consecutive_transient_failures = 0
        self._timeout_warning_shown = False

        self._build()
        self._load_history()
        self._load_shadow_history()
        self._schedule_shadow_refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll_messages)

    def _build(self) -> None:
        add_branded_header(
            self,
            title="Orderflow-Messmodul",
            subtitle=(
                "Read-only: keine Orders und keine Änderung des Momentum-Scores. "
                "Automatische Messreihen plus sichtbarer, read-only 10%-Shadow-Monitor."
            ),
            version=APP_VERSION,
            safety_text="KEINE ECHTEN BROKERORDERS",
        )

        controls = ttk.LabelFrame(self, text="DATENQUELLE UND MESSUNG", padding=10)
        controls.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Checkbutton(
            controls,
            text="Letzte Top-Kandidaten verwenden",
            variable=self.use_latest_var,
        ).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Label(controls, text="oder Symbole:").grid(row=0, column=1, sticky="e", pady=4)
        ttk.Entry(controls, textvariable=self.symbols_var, width=24).grid(
            row=0, column=2, sticky="ew", padx=6, pady=4
        )
        ttk.Label(controls, text="Anzahl:").grid(row=0, column=3, sticky="e", padx=(12, 2), pady=4)
        ttk.Spinbox(controls, from_=1, to=30, textvariable=self.limit_var, width=5).grid(
            row=0, column=4, sticky="w", pady=4
        )
        ttk.Label(controls, text="Tiefe:").grid(row=0, column=5, sticky="e", padx=(12, 2), pady=4)
        ttk.Spinbox(
            controls,
            from_=1,
            to=1,
            textvariable=self.depth_var,
            width=5,
            state="readonly",
        ).grid(row=0, column=6, sticky="w", pady=4)
        ttk.Checkbutton(
            controls,
            text="Fehlerzeilen ausblenden",
            variable=self.hide_errors_var,
            command=self._load_history,
        ).grid(row=0, column=7, sticky="w", padx=(14, 0), pady=4)

        ttk.Label(controls, text="Intervall:").grid(row=1, column=0, sticky="e", pady=4)
        ttk.Spinbox(
            controls,
            from_=5,
            to=300,
            increment=5,
            textvariable=self.interval_var,
            width=7,
        ).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(controls, text="Sekunden").grid(row=1, column=2, sticky="w", padx=(0, 12), pady=4)

        self.auto_start_button = ttk.Button(
            controls,
            text="Automatik starten",
            command=self._start_auto_measurement,
            style="Success.TButton",
        )
        self.auto_start_button.grid(row=1, column=3, columnspan=2, sticky="ew", padx=(6, 6), pady=4)
        self.auto_stop_button = ttk.Button(
            controls,
            text="Automatik stoppen",
            command=self._stop_auto_measurement,
            state="disabled",
            style="Danger.TButton",
        )
        self.auto_stop_button.grid(row=1, column=5, columnspan=2, sticky="ew", padx=6, pady=4)
        self.measure_button = ttk.Button(
            controls,
            text="JETZT MESSEN",
            command=self._start_manual_measurement,
            style="Accent.TButton",
        )
        self.measure_button.grid(row=1, column=7, sticky="ew", padx=(8, 0), pady=4)
        controls.columnconfigure(2, weight=1)

        status_panel = tk.Frame(self, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1)
        status_panel.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(
            status_panel,
            textvariable=self.auto_status_var,
            bg=COLORS["panel"],
            fg=COLORS["green"],
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=12, pady=9)
        tk.Label(
            status_panel,
            textvariable=self.connection_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(18, 8), pady=9)
        tk.Label(
            status_panel,
            textvariable=self.last_success_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side="right", padx=12, pady=9)

        self.main_notebook = ttk.Notebook(self)
        self.main_notebook.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.orderflow_tab = ttk.Frame(self.main_notebook)
        self.shadow_tab = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.orderflow_tab, text="ORDERFLOW LIVE")
        self.main_notebook.add(self.shadow_tab, text="10%-SHADOW TRADES")

        columns = (
            "time", "symbol", "rank", "scanner", "tradeability", "development", "pressure", "quality",
            "spread", "imbalance", "top_imbalance", "micro", "bid_depth", "ask_depth", "warnings",
        )
        frame = ttk.LabelFrame(self.orderflow_tab, text="LIVE-MESSUNGEN", padding=8)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.tree = ttk.Treeview(frame, columns=columns, show="headings")
        labels = {
            "time": "Zeit (UTC)", "symbol": "Symbol", "rank": "Scan-Rang",
            "scanner": "Scanner-Score", "tradeability": "Handelbarkeit",
            "development": "Entwicklung", "pressure": "L2-Druck", "quality": "Qualität", "spread": "Spread",
            "imbalance": "Book-Imbalance", "top_imbalance": "Top-Imbalance",
            "micro": "Microprice-Abstand", "bid_depth": "Gew. Bid-Tiefe",
            "ask_depth": "Gew. Ask-Tiefe", "warnings": "Hinweise",
        }
        widths = {
            "time": 150, "symbol": 70, "rank": 75, "scanner": 95, "tradeability": 125,
            "development": 175, "pressure": 115, "quality": 85, "spread": 90, "imbalance": 105,
            "top_imbalance": 100, "micro": 115, "bid_depth": 105,
            "ask_depth": 105, "warnings": 340,
        }
        for column in columns:
            self.tree.heading(column, text=labels[column])
            self.tree.column(
                column,
                width=widths[column],
                anchor="center" if column != "warnings" else "w",
            )
        self.tree.tag_configure("ok", foreground=COLORS["text"])
        self.tree.tag_configure("positive", foreground=COLORS["green"])
        self.tree.tag_configure("warning", foreground=COLORS["orange"])
        self.tree.tag_configure("error", foreground=COLORS["red"])

        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        footer = tk.Frame(self.orderflow_tab, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1)
        footer.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=12, pady=10)
        ttk.Button(footer, text="HISTORIE AKTUALISIEREN", command=self._load_history).pack(
            side="right", padx=10, pady=6
        )

        self._build_shadow_tab(self.shadow_tab)

    def _build_shadow_tab(self, parent: ttk.Frame) -> None:
        toolbar = ttk.LabelFrame(parent, text="TOP-MOVER 10%-SHADOW · READ-ONLY", padding=8)
        toolbar.pack(fill="x", padx=4, pady=(4, 8))
        ttk.Label(toolbar, text="Ansicht:").pack(side="left", padx=(0, 5))
        filter_box = ttk.Combobox(
            toolbar,
            textvariable=self.shadow_filter_var,
            values=("OFFEN", "GESCHLOSSEN", "ALLE"),
            state="readonly",
            width=15,
        )
        filter_box.pack(side="left", padx=(0, 12))
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self._load_shadow_history())
        ttk.Checkbutton(
            toolbar,
            text="automatisch alle 5 Sekunden",
            variable=self.shadow_auto_var,
            command=self._toggle_shadow_auto_refresh,
        ).pack(side="left", padx=(0, 12))
        ttk.Button(toolbar, text="JETZT AKTUALISIEREN", command=self._load_shadow_history).pack(
            side="left", padx=4
        )
        ttk.Button(toolbar, text="CSV EXPORTIEREN", command=self._export_shadow_history).pack(
            side="left", padx=4
        )
        tk.Label(
            toolbar,
            textvariable=self.shadow_connection_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side="right", padx=8)

        cards = tk.Frame(parent, bg=COLORS["bg"])
        cards.pack(fill="x", padx=4, pady=(0, 8))
        card_specs = (
            ("OFFEN", self.shadow_open_var, COLORS["green"]),
            ("PHASEN", self.shadow_phase_var, COLORS["text"]),
            ("HEUTE", self.shadow_today_var, COLORS["gold"]),
            ("ERGEBNISSE", self.shadow_result_var, COLORS["text"]),
            ("QUOTE", self.shadow_rate_var, COLORS["blue"]),
        )
        for index, (title, variable, color) in enumerate(card_specs):
            card = tk.Frame(cards, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 4, 0))
            tk.Label(
                card, text=title, bg=COLORS["panel"], fg=COLORS["muted"],
                font=("Segoe UI Semibold", 8),
            ).pack(anchor="w", padx=10, pady=(7, 1))
            tk.Label(
                card, textvariable=variable, bg=COLORS["panel"], fg=color,
                font=("Segoe UI Semibold", 12),
            ).pack(anchor="w", padx=10, pady=(0, 8))
            cards.columnconfigure(index, weight=1)

        columns = (
            "symbol", "phase", "status", "opened", "hold", "entry", "current",
            "pnl", "target", "stop", "to_target", "to_stop", "rank", "priority",
            "purchase", "signal", "risk", "rvol", "spread", "guard", "mfe", "mae",
        )
        table_frame = ttk.LabelFrame(parent, text="SICHTBARE SHADOW-POSITIONEN", padding=8)
        table_frame.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        self.shadow_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        labels = {
            "symbol": "Symbol", "phase": "Phase", "status": "Status", "opened": "Eröffnet",
            "hold": "Dauer", "entry": "Einstieg", "current": "Aktuell", "pnl": "P/L",
            "target": "Ziel +10%", "stop": "Stop -7%", "to_target": "bis Ziel",
            "to_stop": "bis Stop", "rank": "Rang", "priority": "Prio",
            "purchase": "Kaufscore", "signal": "Signal", "risk": "Risiko",
            "rvol": "RVOL", "spread": "Spread", "guard": "Entry Guard",
            "mfe": "MFE", "mae": "MAE",
        }
        widths = {
            "symbol": 74, "phase": 58, "status": 105, "opened": 112, "hold": 72,
            "entry": 82, "current": 82, "pnl": 76, "target": 84, "stop": 84,
            "to_target": 76, "to_stop": 76, "rank": 55, "priority": 52,
            "purchase": 82, "signal": 70, "risk": 70, "rvol": 70, "spread": 72,
            "guard": 92, "mfe": 68, "mae": 68,
        }
        for column in columns:
            self.shadow_tree.heading(column, text=labels[column])
            self.shadow_tree.column(column, width=widths[column], anchor="center")
        self.shadow_tree.tag_configure("open_positive", foreground=COLORS["green"])
        self.shadow_tree.tag_configure("open_negative", foreground=COLORS["orange"])
        self.shadow_tree.tag_configure("target", foreground=COLORS["green"])
        self.shadow_tree.tag_configure("stop", foreground=COLORS["red"])
        self.shadow_tree.tag_configure("time", foreground=COLORS["orange"])
        self.shadow_tree.tag_configure("ambiguous", foreground=COLORS["gold"])
        self.shadow_tree.tag_configure("closed", foreground=COLORS["muted"])
        self.shadow_tree.bind("<<TreeviewSelect>>", self._on_shadow_select)
        self.shadow_tree.bind("<Double-1>", self._on_shadow_double_click)

        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.shadow_tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.shadow_tree.xview)
        self.shadow_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.shadow_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        detail = tk.Frame(parent, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1)
        detail.pack(fill="x", padx=4, pady=(0, 4))
        tk.Label(
            detail, textvariable=self.shadow_detail_var, bg=COLORS["panel"], fg=COLORS["text"],
            font=("Segoe UI", 9), anchor="w", justify="left",
        ).pack(fill="x", padx=10, pady=8)

    @staticmethod
    def _shadow_fmt(value: object, decimals: int = 2, suffix: str = "") -> str:
        if value is None:
            return "–"
        try:
            return f"{float(value):.{decimals}f}{suffix}"
        except (TypeError, ValueError):
            return str(value)

    def _load_shadow_history(self) -> None:
        snapshot = self.shadow_reader.snapshot(self.shadow_filter_var.get(), limit=1000)
        self.shadow_connection_var.set(snapshot.status_text)
        self.shadow_open_var.set(f"{snapshot.open_total} / 4")
        self.shadow_phase_var.set(f"RTH {snapshot.open_rth}/4 · PRE {snapshot.open_pre}/1")
        self.shadow_today_var.set(f"{snapshot.opened_today} / 8")
        self.shadow_result_var.set(
            f"Ziel {snapshot.wins} · Stop {snapshot.losses} · Zeit {snapshot.time_exits} · Mehrdeutig {snapshot.ambiguous}"
        )
        rate = snapshot.target_before_stop_rate_pct
        self.shadow_rate_var.set("Ziel vor Stop: –" if rate is None else f"Ziel vor Stop: {rate:.1f}%")

        for item in self.shadow_tree.get_children():
            self.shadow_tree.delete(item)
        self._shadow_rows.clear()

        for row in snapshot.rows:
            item_id = str(row.get("id") or f"{row.get('symbol')}-{len(self._shadow_rows)}")
            pnl = row.get("pnl_pct")
            exit_reason = str(row.get("exit_reason") or "").upper()
            status = str(row.get("status") or "").upper()
            if status == "OPEN":
                tag = "open_positive" if (float(pnl or 0.0) >= 0) else "open_negative"
            elif exit_reason == "TARGET":
                tag = "target"
            elif exit_reason == "STOP":
                tag = "stop"
            elif exit_reason == "TIME":
                tag = "time"
            elif exit_reason == "AMBIGUOUS":
                tag = "ambiguous"
            else:
                tag = "closed"

            self._shadow_rows[item_id] = row
            self.shadow_tree.insert(
                "", tk.END, iid=item_id, tags=(tag,),
                values=(
                    row.get("symbol") or "–",
                    row.get("phase") or "–",
                    row.get("display_status") or "–",
                    row.get("opened_local") or "–",
                    row.get("hold") or "–",
                    self._shadow_fmt(row.get("entry"), 4),
                    self._shadow_fmt(row.get("current"), 4),
                    self._shadow_fmt(pnl, 2, "%"),
                    self._shadow_fmt(row.get("target"), 4),
                    self._shadow_fmt(row.get("stop"), 4),
                    self._shadow_fmt(row.get("target_distance_pct"), 2, "%"),
                    self._shadow_fmt(row.get("stop_distance_pct"), 2, "%"),
                    row.get("rank") if row.get("rank") is not None else "–",
                    row.get("priority") or "–",
                    self._shadow_fmt(row.get("purchase_score"), 1),
                    self._shadow_fmt(row.get("signal_score"), 1),
                    self._shadow_fmt(row.get("risk_score"), 1),
                    self._shadow_fmt(row.get("rvol"), 2),
                    self._shadow_fmt(
                        float(row.get("spread_pct")) * 100.0 if row.get("spread_pct") is not None else None,
                        2, "%"
                    ),
                    row.get("entry_guard") or "–",
                    self._shadow_fmt(row.get("max_favorable_pct"), 2, "%"),
                    self._shadow_fmt(row.get("max_adverse_pct"), 2, "%"),
                ),
            )

        if not snapshot.rows:
            self.shadow_detail_var.set(
                f"Keine Positionen für Ansicht {self.shadow_filter_var.get()}. Datenbank: {snapshot.database}"
            )
        else:
            self.shadow_detail_var.set(
                f"{len(snapshot.rows)} Position(en) angezeigt · ATH nur beobachtet: {snapshot.observed_ath} · "
                "Doppelklick übernimmt das Symbol in ORDERFLOW LIVE."
            )

    def _on_shadow_select(self, _event: object = None) -> None:
        selected = self.shadow_tree.selection()
        if not selected:
            return
        row = self._shadow_rows.get(selected[0])
        if not row:
            return
        self.shadow_detail_var.set(
            f"{row.get('symbol')} · {row.get('phase')} · {row.get('display_status')} · "
            f"P/L {self._shadow_fmt(row.get('pnl_pct'), 2, '%')} · "
            f"MFE {self._shadow_fmt(row.get('max_favorable_pct'), 2, '%')} · "
            f"MAE {self._shadow_fmt(row.get('max_adverse_pct'), 2, '%')} · "
            f"Entry Guard {row.get('entry_guard') or '–'}"
        )

    def _on_shadow_double_click(self, _event: object = None) -> None:
        selected = self.shadow_tree.selection()
        if not selected:
            return
        row = self._shadow_rows.get(selected[0])
        if not row or not row.get("symbol"):
            return
        symbol = str(row["symbol"])
        self.use_latest_var.set(False)
        self.symbols_var.set(symbol)
        self.main_notebook.select(self.orderflow_tab)
        self.status_var.set(
            f"{symbol} aus dem 10%-Shadow übernommen. Mit JETZT MESSEN wird eine read-only Orderflow-Messung gestartet."
        )

    def _export_shadow_history(self) -> None:
        try:
            path, count = self.shadow_reader.export()
        except Exception as exc:
            messagebox.showerror("10%-Shadow-Export", str(exc))
            return
        messagebox.showinfo(
            "10%-Shadow-Export",
            f"Export erfolgreich.\n\nPositionen: {count}\nDatei:\n{path}",
        )

    def _toggle_shadow_auto_refresh(self) -> None:
        if self.shadow_auto_var.get():
            self._schedule_shadow_refresh()
        else:
            self._cancel_shadow_refresh()

    def _schedule_shadow_refresh(self) -> None:
        self._cancel_shadow_refresh()
        if self.shadow_auto_var.get():
            self._shadow_job_id = self.after(5000, self._shadow_refresh_tick)

    def _shadow_refresh_tick(self) -> None:
        self._shadow_job_id = None
        self._load_shadow_history()
        self._schedule_shadow_refresh()

    def _cancel_shadow_refresh(self) -> None:
        if self._shadow_job_id is not None:
            try:
                self.after_cancel(self._shadow_job_id)
            except Exception:
                logger.debug("10%-Shadow-Timer war bereits beendet", exc_info=True)
            self._shadow_job_id = None

    def _contexts(self) -> list[ScannerContext]:
        if self.use_latest_var.get():
            contexts = self.storage.latest_top_contexts(limit=self.limit_var.get())
            if contexts:
                return contexts
        symbols = sorted(
            {
                value.strip().upper()
                for value in self.symbols_var.get().replace(";", ",").split(",")
                if value.strip()
            }
        )
        if not symbols and self.settings.mode == "mock":
            symbols = ["ALFA", "BETA", "CYGN"]
        return [ScannerContext(symbol=symbol) for symbol in symbols[: self.limit_var.get()]]

    def _start_manual_measurement(self) -> None:
        if self.auto_state.active:
            messagebox.showinfo(
                "Automatik läuft",
                "Stoppe zuerst die automatische Messreihe, bevor du manuell misst.",
            )
            return
        self._begin_measurement(automatic=False)

    def _start_auto_measurement(self) -> None:
        if self.auto_state.active:
            return
        try:
            self.auto_state.start(self.interval_var.get())
        except ValueError as exc:
            messagebox.showerror("Ungültiges Intervall", str(exc))
            return

        if not self._contexts():
            self.auto_state.stop()
            self.status_var.set("Keine Kandidaten: Momentum-Scanner ausführen oder Symbole eingeben.")
            self.connection_var.set("Marktdaten: wartet auf Kandidaten")
            return

        self.interval_var.set(self.auto_state.interval_seconds)
        self.auto_start_button.configure(state="disabled")
        self.auto_stop_button.configure(state="normal")
        self.measure_button.configure(state="disabled")
        self.auto_status_var.set(
            f"AUTOMATIK: AKTIV · {self.auto_state.interval_seconds} s · Zyklus 0"
        )
        self.status_var.set("Automatische Messreihe gestartet.")
        self.connection_var.set("Marktdaten: Verbindung wird geprüft …")
        self._cancel_auto_job()
        self._begin_measurement(automatic=True)

    def _stop_auto_measurement(self) -> None:
        was_active = self.auto_state.active
        self.auto_state.stop()
        self._cancel_auto_job()
        self.auto_start_button.configure(state="normal")
        self.auto_stop_button.configure(state="disabled")
        if not self._measurement_running:
            self.measure_button.configure(state="normal")
        self.auto_status_var.set("AUTOMATIK: AUS")
        if was_active:
            if self._measurement_running:
                self.status_var.set(
                    "Automatik gestoppt. Die laufende Messung wird noch abgeschlossen."
                )
            else:
                self.status_var.set("Automatische Messreihe gestoppt.")

    def _begin_measurement(self, automatic: bool) -> None:
        if self._measurement_running:
            return
        contexts = self._contexts()
        if not contexts:
            if automatic:
                self._stop_auto_measurement()
            self.status_var.set("Keine Kandidaten: Momentum-Scanner ausführen oder Symbole eingeben.")
            return

        self._measurement_running = True
        self.measure_button.configure(state="disabled")
        mode = "Automatik" if automatic else "Einzelmessung"
        self.status_var.set(f"{mode}: Messe {len(contexts)} Symbol(e) …")
        threading.Thread(
            target=self._measure_worker,
            args=(contexts, 1),
            daemon=True,
        ).start()

    def _measure_worker(self, contexts: list[ScannerContext], depth: int) -> None:
        try:
            monitor = OrderflowMonitor(_provider(self.settings), self.storage)
            rows = monitor.measure_many(contexts, depth=depth)
            self.messages.put(("done", rows))
        except Exception as exc:
            if is_transient_network_error(exc):
                logger.warning("Orderflow-Messung vorübergehend nicht erreichbar: %s", exc)
            else:
                logger.exception("Orderflow-Messung konnte nicht gestartet werden")
            self.messages.put(("error", exc))

    def _schedule_next_auto_measurement(self, delay_seconds: int | None = None) -> None:
        self._cancel_auto_job()
        if not self.auto_state.active:
            return
        delay = delay_seconds or self.auto_state.interval_seconds
        self._auto_job_id = self.after(delay * 1000, self._auto_tick)

    def _auto_tick(self) -> None:
        self._auto_job_id = None
        if self.auto_state.active and not self._measurement_running:
            self._begin_measurement(automatic=True)

    def _cancel_auto_job(self) -> None:
        if self._auto_job_id is not None:
            try:
                self.after_cancel(self._auto_job_id)
            except Exception:
                logger.debug("Automatik-Timer war bereits beendet", exc_info=True)
            finally:
                self._auto_job_id = None

    def _reset_connection_failures(self) -> None:
        self._consecutive_transient_failures = 0
        self._timeout_warning_shown = False
        now = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        self.connection_var.set("Marktdaten: VERBUNDEN")
        self.last_success_var.set(f"Letzter Erfolg: {now}")

    def _handle_transient_failure(self, details: str = "") -> None:
        self._consecutive_transient_failures += 1
        delay = _retry_delay(self._consecutive_transient_failures)
        self.connection_var.set(
            f"Marktdaten verzögert · Versuch {self._consecutive_transient_failures} · Retry in {delay} s"
        )
        self.status_var.set(
            "Webull antwortet vorübergehend nicht. Die Automatik bleibt aktiv und versucht es erneut."
        )
        if self.auto_state.active:
            self._schedule_next_auto_measurement(delay)
        else:
            self.measure_button.configure(state="normal")

        # Nur ein einziges Hinweisfenster pro Störungsphase – und erst nach 3 Fehlzyklen.
        if self._consecutive_transient_failures >= 3 and not self._timeout_warning_shown:
            self._timeout_warning_shown = True
            messagebox.showwarning(
                "Marktdaten verzögert",
                "Webull hat mehrfach nicht rechtzeitig geantwortet.\n\n"
                "Die Messautomatik läuft weiter und versucht es automatisch erneut. "
                "Es wurden keine Orders gesendet und keine Scanner-Scores verändert."
                + (f"\n\nTechnischer Hinweis: {details[:350]}" if details else ""),
            )

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "done":
                    self._measurement_running = False
                    rows = list(payload)  # type: ignore[arg-type]
                    self._load_history()
                    error_rows = [row for row in rows if getattr(row, "data_quality", "") == "FEHLER"]
                    successful_rows = [row for row in rows if getattr(row, "data_quality", "") != "FEHLER"]
                    error_texts = [_measurement_error_text(row) for row in error_rows]
                    all_transient = bool(error_rows) and all(
                        is_transient_network_error(text) for text in error_texts if text
                    ) and all(bool(text) for text in error_texts)

                    if successful_rows:
                        self._reset_connection_failures()
                        if self.auto_state.active:
                            cycle = self.auto_state.mark_completed()
                            self.auto_status_var.set(
                                "AUTOMATIK: AKTIV · "
                                f"{self.auto_state.interval_seconds} s · Zyklus {cycle}"
                            )
                            if error_rows:
                                self.status_var.set(
                                    f"Zyklus {cycle}: {len(successful_rows)} erfolgreich, "
                                    f"{len(error_rows)} vorübergehend nicht verfügbar."
                                )
                            else:
                                self.status_var.set(
                                    f"Zyklus {cycle}: {len(rows)} Messung(en) gespeichert. "
                                    f"Nächster Zyklus in {self.auto_state.interval_seconds} Sekunden."
                                )
                            self._schedule_next_auto_measurement()
                        else:
                            self.measure_button.configure(state="normal")
                            self.status_var.set(
                                f"{len(successful_rows)} Messung(en) gespeichert"
                                + (f", {len(error_rows)} Fehlerzeile(n)." if error_rows else ".")
                            )
                    elif all_transient:
                        self._handle_transient_failure(" | ".join(error_texts))
                    else:
                        self.connection_var.set("Marktdaten: FEHLER")
                        if self.auto_state.active:
                            cycle = self.auto_state.mark_completed()
                            self.auto_status_var.set(
                                f"AUTOMATIK: AKTIV · Zyklus {cycle} mit Datenfehlern"
                            )
                            self.status_var.set(
                                "Messung lieferte nur Fehlerzeilen. Nächster regulärer Zyklus bleibt geplant."
                            )
                            self._schedule_next_auto_measurement()
                        else:
                            self.measure_button.configure(state="normal")
                            self.status_var.set("Messung lieferte keine verwendbaren Orderflow-Daten.")

                elif kind == "error":
                    self._measurement_running = False
                    exc = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
                    if is_transient_network_error(exc):
                        self._handle_transient_failure(str(exc))
                    else:
                        self._stop_auto_measurement()
                        self.measure_button.configure(state="normal")
                        self.connection_var.set("Marktdaten: FEHLER")
                        self.status_var.set("Messung konnte nicht gestartet werden. Automatik wurde gestoppt.")
                        messagebox.showerror("Orderflow-Fehler", str(exc))
        except queue.Empty:
            pass
        self.after(150, self._poll_messages)

    def _load_history(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        rows = self.storage.latest_measurements(limit=300)
        developments = analyse_history(rows)
        for row in rows:
            if self.hide_errors_var.get() and row.get("data_quality") == "FEHLER":
                continue
            development = developments.get(row.get("id"))
            reason = row.get("liquidity_reason") or ""
            warnings = list(row.get("warnings") or [])
            if development and development.details:
                warnings.insert(0, f"Verlauf: {development.details}")
            if reason and reason not in warnings:
                warnings.insert(0, reason)

            tag = "ok"
            if row.get("data_quality") == "FEHLER":
                tag = "error"
            elif str(row.get("liquidity_status") or "").upper() in {"NICHT HANDELBAR", "ILLIQUIDE"}:
                tag = "warning"
            elif (row.get("book_imbalance") or 0) > 0.25:
                tag = "positive"

            self.tree.insert(
                "",
                tk.END,
                tags=(tag,),
                values=(
                    row["timestamp"], row["symbol"], row.get("scan_rank") or "–",
                    _fmt(row.get("scanner_score")), row.get("liquidity_status") or "–",
                    development.label if development else "–",
                    row["pressure"], row["data_quality"],
                    _fmt(row.get("spread_bps"), 1, " bp"),
                    _fmt(row.get("book_imbalance"), 3),
                    _fmt(row.get("top_imbalance"), 3),
                    _fmt(row.get("microprice_edge_bps"), 2, " bp"),
                    _fmt(row.get("weighted_bid_depth"), 0),
                    _fmt(row.get("weighted_ask_depth"), 0),
                    "; ".join(warnings),
                ),
            )

    def _on_close(self) -> None:
        self.auto_state.stop()
        self._cancel_auto_job()
        self._cancel_shadow_refresh()
        self.destroy()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    OrderflowMonitorApp().mainloop()


if __name__ == "__main__":
    main()
