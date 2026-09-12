from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import tkinter as tk
from tkinter import BooleanVar, IntVar, StringVar, Tk, Toplevel, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from spike_scanner.config import Settings
from spike_scanner.german import de_number, feature_label, format_feature, probability_text
from spike_scanner.orderflow_launcher import OrderflowLaunchError, OrderflowProcessController
from spike_scanner.providers import (
    MockMarketDataProvider,
    WebullMarketDataProvider,
    is_transient_network_error,
)
from spike_scanner.scanner import MomentumScanner
from spike_scanner.storage import Storage
from spike_scanner.scoring import (
    MIN_TOP_SIGNAL_SCORE,
    combined_score,
    is_top_eligible,
    ranking_score,
    purchase_assessment,
    signal_age_minutes,
)

logger = logging.getLogger(__name__)

APP_TITLE = "US Momentum Scanner"
APP_VERSION = "0.6.0"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
if not ASSET_DIR.exists() and getattr(sys, "_MEIPASS", None):
    packaged = Path(sys._MEIPASS) / "spike_scanner" / "assets"
    ASSET_DIR = packaged if packaged.exists() else Path(sys._MEIPASS) / "assets"
APP_LOGO_PATH = ASSET_DIR / "emblem_app.png"
APP_ICON_PATH = ASSET_DIR / "emblem_icon.png"
APP_BANNER_PATH = ASSET_DIR / "brand_banner.png"
MODE_LABELS = {
    "Testdaten (Mock-Modus)": "mock",
    "Webull Live-Daten": "webull",
}

DARK = {
    "bg": "#111827",
    "panel": "#182235",
    "panel_alt": "#202c42",
    "fg": "#f3f4f6",
    "muted": "#9ca3af",
    "accent": "#38bdf8",
    "accent_active": "#0ea5e9",
    "success": "#22c55e",
    "warning": "#f59e0b",
    "danger": "#ef4444",
    "border": "#334155",
    "entry": "#0f172a",
    "selection": "#075985",
}

LIGHT = {
    "bg": "#f3f6fb",
    "panel": "#ffffff",
    "panel_alt": "#e8eef7",
    "fg": "#172033",
    "muted": "#64748b",
    "accent": "#0284c7",
    "accent_active": "#0369a1",
    "success": "#15803d",
    "warning": "#b45309",
    "danger": "#b91c1c",
    "border": "#cbd5e1",
    "entry": "#ffffff",
    "selection": "#bae6fd",
}


def _parse_utc(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _new_york_zone():
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        # Fallback nur für ungewöhnliche Python-Installationen ohne tzdata.
        # Der Installer installiert tzdata, damit Sommer-/Winterzeit korrekt bleibt.
        return timezone.utc


def _format_recommendation_time(value: object, *, eastern: bool = False) -> str:
    dt = _parse_utc(value)
    if dt is None:
        return "–"
    target = _new_york_zone() if eastern else datetime.now().astimezone().tzinfo
    return dt.astimezone(target).strftime("%d.%m.%Y %H:%M:%S")


def _signal_age(value: object) -> str:
    dt = _parse_utc(value)
    if dt is None:
        return "–"
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}T {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _market_phase(value: object) -> str:
    dt = _parse_utc(value)
    if dt is None:
        return "–"
    et = dt.astimezone(_new_york_zone())
    if et.weekday() >= 5:
        return "OFF"
    minute = et.hour * 60 + et.minute
    if 4 * 60 <= minute < 9 * 60 + 30:
        return "PRE"
    if 9 * 60 + 30 <= minute < 16 * 60:
        return "RTH"
    if 16 * 60 <= minute < 20 * 60:
        return "ATH"
    return "OFF"


class TextQueueHandler(logging.Handler):
    def __init__(self, messages: queue.Queue[tuple[str, object]]) -> None:
        super().__init__()
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.put(("log", self.format(record)))


class MomentumScannerApp(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} – Version {APP_VERSION}")
        self.geometry("1480x900")
        self.minsize(1100, 700)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._logo_image = None
        self._icon_image = None
        self._configure_branding()

        self.project_dir = Path.cwd()
        self.settings = Settings.from_env()
        self.settings.ensure_directories()
        self.storage = Storage(self.settings.database_path)
        self.orderflow_controller = OrderflowProcessController(self.project_dir)

        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.auto_after_id: str | None = None
        self.auto_running = False
        self.scan_in_progress = False
        self.latest_result = None
        self.consecutive_network_errors = 0

        self.mode_var = StringVar(
            value="Webull Live-Daten" if self.settings.mode == "webull" else "Testdaten (Mock-Modus)"
        )
        self.universe_var = IntVar(value=self.settings.universe_size)
        self.top_var = IntVar(value=self.settings.top_n)
        self.interval_var = IntVar(value=self.settings.scan_interval_seconds)
        self.dark_var = BooleanVar(value=True)
        self.order_book_var = BooleanVar(value=self.settings.use_order_book)
        self.status_var = StringVar(value="Bereit")
        self.last_scan_var = StringVar(value="Noch kein Scan in dieser Sitzung")
        self.countdown_var = StringVar(value="")
        self.learning_summary_var = StringVar(value="Lernstatus wird geladen …")
        self.orderflow_status_var = StringVar(value="Orderflow: AUS")

        self._configure_logging()
        self._build_menu()
        self._build_layout()
        self._apply_theme()
        self._load_latest_from_storage()
        self.after(100, self._process_messages)
        self.orderflow_after_id = self.after(500, self._refresh_orderflow_status)

    def _configure_branding(self) -> None:
        try:
            if APP_ICON_PATH.exists():
                self._icon_image = tk.PhotoImage(file=str(APP_ICON_PATH))
                self.iconphoto(True, self._icon_image)
        except Exception:
            self._icon_image = None

    def _load_header_banner(self, target_width: int = 700):
        try:
            if APP_BANNER_PATH.exists():
                image = tk.PhotoImage(file=str(APP_BANNER_PATH))
            elif APP_LOGO_PATH.exists():
                image = tk.PhotoImage(file=str(APP_LOGO_PATH))
            else:
                return None
            src_w = max(image.width(), 1)
            factor = max(1, round(src_w / target_width))
            image = image.subsample(factor, factor)
            self._logo_image = image
            return image
        except Exception:
            return None

    def _configure_logging(self) -> None:
        handler = TextQueueHandler(self.messages)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        self._log_handler = handler

    def _build_menu(self) -> None:
        import tkinter as tk

        self.menu_bar = tk.Menu(self)
        file_menu = tk.Menu(self.menu_bar, tearoff=False)
        file_menu.add_command(label="Einmal scannen", command=self.start_single_scan)
        file_menu.add_command(label="Letzte CSV öffnen", command=self._open_latest_csv)
        file_menu.add_command(label="Datenordner öffnen", command=self._open_data_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Beenden", command=self._on_close)
        self.menu_bar.add_cascade(label="Datei", menu=file_menu)

        view_menu = tk.Menu(self.menu_bar, tearoff=False)
        view_menu.add_checkbutton(label="Dunkelmodus", variable=self.dark_var, command=self._apply_theme)
        view_menu.add_command(label="Daten neu laden", command=self._load_latest_from_storage)
        self.menu_bar.add_cascade(label="Ansicht", menu=view_menu)

        settings_menu = tk.Menu(self.menu_bar, tearoff=False)
        settings_menu.add_command(label="Webull und Filter …", command=self._open_settings_dialog)
        settings_menu.add_command(label=".env-Datei öffnen", command=self._open_env_file)
        self.menu_bar.add_cascade(label="Einstellungen", menu=settings_menu)

        tools_menu = tk.Menu(self.menu_bar, tearoff=False)
        tools_menu.add_command(label="Orderflow-Monitor öffnen", command=self._open_orderflow_monitor)
        tools_menu.add_command(label="Orderflow-Monitor beenden", command=self._stop_orderflow_monitor)
        self.menu_bar.add_cascade(label="Werkzeuge", menu=tools_menu)

        help_menu = tk.Menu(self.menu_bar, tearoff=False)
        help_menu.add_command(label="Hinweise", command=self._show_help)
        help_menu.add_command(label="Über das Programm", command=self._show_about)
        self.menu_bar.add_cascade(label="Hilfe", menu=help_menu)
        self.config(menu=self.menu_bar)

    def _build_layout(self) -> None:
        self.main = ttk.Frame(self, padding=14)
        self.main.pack(fill="both", expand=True)

        header = ttk.Frame(self.main, style="Panel.TFrame", padding=16)
        header.pack(fill="x", pady=(0, 12))

        branding = ttk.Frame(header, style="Panel.TFrame")
        branding.pack(side="left", fill="x", expand=True)
        banner = self._load_header_banner()
        if banner is not None:
            ttk.Label(branding, image=banner, style="Panel.TLabel").pack(anchor="w", pady=(0, 6))
        else:
            ttk.Label(branding, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            branding,
            text="Prospektive Analyse auffälliger US-Aktien – keine automatische Orderausführung",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        controls = ttk.Frame(header, style="Panel.TFrame")
        controls.pack(side="right")
        ttk.Checkbutton(
            controls,
            text="Dunkelmodus",
            variable=self.dark_var,
            command=self._apply_theme,
            style="Switch.TCheckbutton",
        ).grid(row=0, column=0, padx=(0, 10))
        ttk.Button(controls, text="Einstellungen", command=self._open_settings_dialog).grid(row=0, column=1)
        ttk.Label(controls, text=f"Version {APP_VERSION}", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, pady=(8, 0)
        )

        scan_panel = ttk.Frame(self.main, style="Panel.TFrame", padding=14)
        scan_panel.pack(fill="x", pady=(0, 12))

        ttk.Label(scan_panel, text="Datenquelle", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.mode_combo = ttk.Combobox(
            scan_panel,
            textvariable=self.mode_var,
            values=list(MODE_LABELS),
            state="readonly",
            width=23,
        )
        self.mode_combo.grid(row=1, column=0, padx=(0, 12), sticky="w")

        ttk.Label(scan_panel, text="Untersuchtes Universum", style="Panel.TLabel").grid(row=0, column=1, sticky="w")
        self.universe_spin = ttk.Spinbox(scan_panel, from_=3, to=250, textvariable=self.universe_var, width=10)
        self.universe_spin.grid(row=1, column=1, padx=(0, 12), sticky="w")

        ttk.Label(scan_panel, text="Top-Kandidaten", style="Panel.TLabel").grid(row=0, column=2, sticky="w")
        self.top_spin = ttk.Spinbox(scan_panel, from_=1, to=10, textvariable=self.top_var, width=10)
        self.top_spin.grid(row=1, column=2, padx=(0, 12), sticky="w")

        ttk.Label(scan_panel, text="Intervall in Sekunden", style="Panel.TLabel").grid(row=0, column=3, sticky="w")
        self.interval_spin = ttk.Spinbox(scan_panel, from_=30, to=86400, increment=30, textvariable=self.interval_var, width=12)
        self.interval_spin.grid(row=1, column=3, padx=(0, 12), sticky="w")

        self.scan_button = ttk.Button(scan_panel, text="Jetzt scannen", style="Accent.TButton", command=self.start_single_scan)
        self.scan_button.grid(row=1, column=4, padx=(4, 8))
        self.auto_button = ttk.Button(scan_panel, text="Automatik starten", command=self.start_auto_scan)
        self.auto_button.grid(row=1, column=5, padx=8)
        self.stop_button = ttk.Button(scan_panel, text="Stoppen", command=self.stop_auto_scan, state="disabled")
        self.stop_button.grid(row=1, column=6, padx=8)
        self.learn_button = ttk.Button(
            scan_panel, text="Lernen prüfen", command=self.start_learning_cycle
        )
        self.learn_button.grid(row=1, column=7, padx=(8, 0))

        status_panel = ttk.Frame(self.main, style="Panel.TFrame", padding=(14, 9))
        status_panel.pack(fill="x", pady=(0, 12))
        ttk.Label(status_panel, text="Status:", style="PanelBold.TLabel").pack(side="left")
        self.status_label = ttk.Label(status_panel, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.pack(side="left", padx=(8, 20))
        ttk.Label(status_panel, textvariable=self.last_scan_var, style="Muted.TLabel").pack(side="left")

        orderflow_controls = ttk.Frame(status_panel, style="Panel.TFrame")
        orderflow_controls.pack(side="right")
        self.orderflow_status_label = ttk.Label(
            orderflow_controls,
            textvariable=self.orderflow_status_var,
            style="Muted.TLabel",
        )
        self.orderflow_status_label.pack(side="left", padx=(12, 8))
        self.orderflow_open_button = ttk.Button(
            orderflow_controls,
            text="Orderflow öffnen",
            command=self._open_orderflow_monitor,
        )
        self.orderflow_open_button.pack(side="left", padx=(0, 6))
        self.orderflow_stop_button = ttk.Button(
            orderflow_controls,
            text="Orderflow beenden",
            command=self._stop_orderflow_monitor,
            state="disabled",
        )
        self.orderflow_stop_button.pack(side="left")
        ttk.Label(
            orderflow_controls,
            textvariable=self.countdown_var,
            style="Accent.TLabel",
        ).pack(side="left", padx=(12, 0))

        self.notebook = ttk.Notebook(self.main)
        self.notebook.pack(fill="both", expand=True)

        self.top_tab = ttk.Frame(self.notebook, padding=10)
        self.all_tab = ttk.Frame(self.notebook, padding=10)
        self.details_tab = ttk.Frame(self.notebook, padding=10)
        self.learning_tab = ttk.Frame(self.notebook, padding=10)
        self.log_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.top_tab, text="Top-Kandidaten")
        self.notebook.add(self.all_tab, text="Gesamtes Universum")
        self.notebook.add(self.details_tab, text="Merkmale")
        self.notebook.add(self.learning_tab, text="24-h-Lernstatus")
        self.notebook.add(self.log_tab, text="Protokoll")

        self.top_tree = self._create_candidate_tree(self.top_tab, include_selected=False)
        self.all_tree = self._create_candidate_tree(self.all_tab, include_selected=True)
        self.top_tree.bind("<<TreeviewSelect>>", self._candidate_selected)
        self.all_tree.bind("<<TreeviewSelect>>", self._candidate_selected)
        self.top_tree.bind("<Double-1>", lambda _event: self.notebook.select(self.details_tab))
        self.all_tree.bind("<Double-1>", lambda _event: self.notebook.select(self.details_tab))

        detail_header = ttk.Frame(self.details_tab)
        detail_header.pack(fill="x", pady=(0, 8))
        ttk.Label(detail_header, text="Ausgewählte Aktie:", style="PanelBold.TLabel").pack(side="left")
        self.detail_symbol_var = StringVar(value="–")
        ttk.Label(detail_header, textvariable=self.detail_symbol_var, style="DetailSymbol.TLabel").pack(side="left", padx=8)
        ttk.Label(
            detail_header,
            text="Prozentwerte beziehen sich auf den jeweiligen Messzeitraum.",
            style="Muted.TLabel",
        ).pack(side="right")

        detail_columns = ("merkmal", "wert")
        self.detail_tree = ttk.Treeview(self.details_tab, columns=detail_columns, show="headings")
        self.detail_tree.heading("merkmal", text="Merkmal")
        self.detail_tree.heading("wert", text="Wert")
        self.detail_tree.column("merkmal", width=430, anchor="w")
        self.detail_tree.column("wert", width=250, anchor="e")
        detail_scroll = ttk.Scrollbar(self.details_tab, orient="vertical", command=self.detail_tree.yview)
        self.detail_tree.configure(yscrollcommand=detail_scroll.set)
        self.detail_tree.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")

        learning_header = ttk.Frame(self.learning_tab, style="Panel.TFrame", padding=12)
        learning_header.pack(fill="x", pady=(0, 10))
        ttk.Label(
            learning_header,
            text="Automatischer prospektiver 24-Stunden-Lernkreislauf",
            style="PanelBold.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            learning_header,
            textvariable=self.learning_summary_var,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(
            learning_header,
            text=(
                "Neue Modelle werden nur übernommen, wenn sie auf einem späteren "
                "Testzeitraum messbar besser sind. Eine hohe Trefferquote ist nicht garantiert."
            ),
            style="Muted.TLabel",
            wraplength=1100,
        ).pack(anchor="w", pady=(5, 0))

        learning_columns = (
            "ziel", "status", "daten", "gewinner", "basisrate", "ap",
            "brier", "top10", "top3", "modell", "training"
        )
        self.learning_tree = ttk.Treeview(
            self.learning_tab, columns=learning_columns, show="headings"
        )
        learning_headings = {
            "ziel": "Kursziel in 24 h",
            "status": "Modellstatus",
            "daten": "Fälle",
            "gewinner": "Positive Fälle",
            "basisrate": "Basisrate",
            "ap": "Average Precision",
            "brier": "Brier-Fehler",
            "top10": "Präzision Top 10 %",
            "top3": "Ø Tages-Top-3",
            "modell": "Modelltyp",
            "training": "Letztes akzeptiertes Training",
        }
        learning_widths = {
            "ziel": 130, "status": 125, "daten": 75, "gewinner": 100,
            "basisrate": 95, "ap": 120, "brier": 100, "top10": 125,
            "top3": 110, "modell": 155, "training": 185,
        }
        for column in learning_columns:
            self.learning_tree.heading(column, text=learning_headings[column])
            self.learning_tree.column(
                column, width=learning_widths[column], anchor="center"
            )
        learning_scroll = ttk.Scrollbar(
            self.learning_tab, orient="horizontal", command=self.learning_tree.xview
        )
        self.learning_tree.configure(xscrollcommand=learning_scroll.set)
        self.learning_tree.pack(fill="both", expand=True)
        learning_scroll.pack(fill="x")

        self.log_text = ScrolledText(self.log_tab, wrap="word", font=("Consolas", 10), state="disabled")
        self.log_text.pack(fill="both", expand=True)

        footer = ttk.Frame(self.main, padding=(4, 8, 4, 0))
        footer.pack(fill="x")
        ttk.Label(
            footer,
            text=(
                f"Top-Kandidaten erst ab Signal-Score {MIN_TOP_SIGNAL_SCORE:.0f}. "
                "Kauf-Score kombiniert Gesamt, Ziel-P, Signal, Frische und Ausführungsqualität; Forschungswert."
            ),
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Button(footer, text="CSV öffnen", command=self._open_latest_csv).pack(side="right")
        ttk.Button(footer, text="Neu laden", command=self._load_latest_from_storage).pack(side="right", padx=8)

    def _probability_columns(self) -> list[tuple[str, float]]:
        return [
            (f"p_{int(round(threshold * 100))}", threshold)
            for threshold in self.settings.learning_thresholds
        ]

    def _create_candidate_tree(self, parent, include_selected: bool) -> ttk.Treeview:
        columns = [
            "rang", "symbol", "empfehlung_lokal", "empfehlung_et", "alter",
            "phase", "kurs", "signal", "risiko", "gesamt", "ranking", "kauf", "kaufqualitaet"
        ]
        columns.extend(column for column, _ in self._probability_columns())
        columns.append("gruende")
        if include_selected:
            columns.append("auswahl")
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "rang": "Rang",
            "symbol": "Kürzel",
            "empfehlung_lokal": "Erste Empfehlung (lokal)",
            "empfehlung_et": "Erste Empfehlung (ET)",
            "alter": "Signal-Alter",
            "phase": "Phase",
            "kurs": "Kurs (USD)",
            "signal": "Signal-Score",
            "risiko": "Risiko-Score",
            "gesamt": "Gesamt-Score",
            "ranking": "Ranking-Score",
            "kauf": "Kauf-Score",
            "kaufqualitaet": "Kauf-Qualität",
            "gruende": "Wichtigste Gründe",
            "auswahl": "Top-Auswahl",
        }
        widths = {
            "rang": 65, "symbol": 85,
            "empfehlung_lokal": 165, "empfehlung_et": 165, "alter": 95,
            "phase": 65, "kurs": 110, "signal": 110,
            "risiko": 110, "gesamt": 110, "ranking": 115, "kauf": 105, "kaufqualitaet": 125, "gruende": 560, "auswahl": 100,
        }
        anchors = {
            "rang": "center", "symbol": "center",
            "empfehlung_lokal": "center", "empfehlung_et": "center",
            "alter": "center", "phase": "center", "kurs": "e",
            "signal": "e", "risiko": "e", "gesamt": "e", "ranking": "e", "kauf": "e", "kaufqualitaet": "center", "gruende": "w",
            "auswahl": "center",
        }
        for column, threshold in self._probability_columns():
            headings[column] = f"P(≥ +{threshold:.0%} / 24 h)"
            widths[column] = 135
            anchors[column] = "center"
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(
                column, width=widths[column], minwidth=55, anchor=anchors[column]
            )
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _apply_theme(self) -> None:
        import tkinter as tk

        colors = DARK if self.dark_var.get() else LIGHT
        self.colors = colors
        self.configure(bg=colors["bg"])
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=colors["bg"], foreground=colors["fg"], fieldbackground=colors["entry"], bordercolor=colors["border"], lightcolor=colors["border"], darkcolor=colors["border"], font=("Segoe UI", 10))
        style.configure("TFrame", background=colors["bg"])
        style.configure("Panel.TFrame", background=colors["panel"])
        style.configure("TLabel", background=colors["bg"], foreground=colors["fg"])
        style.configure("Panel.TLabel", background=colors["panel"], foreground=colors["fg"])
        style.configure("PanelBold.TLabel", background=colors["panel"], foreground=colors["fg"], font=("Segoe UI Semibold", 10))
        style.configure("Title.TLabel", background=colors["panel"], foreground=colors["fg"], font=("Segoe UI Semibold", 22))
        style.configure("Muted.TLabel", background=colors["panel"], foreground=colors["muted"])
        style.configure("Status.TLabel", background=colors["panel"], foreground=colors["success"], font=("Segoe UI Semibold", 10))
        style.configure("Accent.TLabel", background=colors["panel"], foreground=colors["accent"], font=("Segoe UI Semibold", 10))
        style.configure("DetailSymbol.TLabel", background=colors["bg"], foreground=colors["accent"], font=("Segoe UI Semibold", 14))
        style.configure("TButton", background=colors["panel_alt"], foreground=colors["fg"], padding=(12, 7), borderwidth=1)
        style.map("TButton", background=[("active", colors["border"]), ("disabled", colors["panel"])], foreground=[("disabled", colors["muted"])])
        style.configure("Accent.TButton", background=colors["accent"], foreground="#ffffff", font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", colors["accent_active"])])
        style.configure("TCheckbutton", background=colors["panel"], foreground=colors["fg"])
        style.configure("Switch.TCheckbutton", background=colors["panel"], foreground=colors["fg"])
        style.configure("TCombobox", fieldbackground=colors["entry"], background=colors["panel_alt"], foreground=colors["fg"], arrowcolor=colors["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", colors["entry"])], foreground=[("readonly", colors["fg"])])
        style.configure("TSpinbox", fieldbackground=colors["entry"], foreground=colors["fg"], arrowcolor=colors["fg"])
        style.configure("TNotebook", background=colors["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=colors["panel_alt"], foreground=colors["muted"], padding=(16, 9))
        style.map("TNotebook.Tab", background=[("selected", colors["panel"])], foreground=[("selected", colors["accent"])])
        style.configure("Treeview", background=colors["panel"], fieldbackground=colors["panel"], foreground=colors["fg"], rowheight=30, bordercolor=colors["border"], borderwidth=1)
        style.configure("Treeview.Heading", background=colors["panel_alt"], foreground=colors["fg"], font=("Segoe UI Semibold", 10), padding=(6, 7))
        style.map("Treeview", background=[("selected", colors["selection"])], foreground=[("selected", colors["fg"])])
        style.configure("Vertical.TScrollbar", background=colors["panel_alt"], troughcolor=colors["panel"])
        style.configure("Horizontal.TScrollbar", background=colors["panel_alt"], troughcolor=colors["panel"])

        self.log_text.configure(bg=colors["entry"], fg=colors["fg"], insertbackground=colors["fg"], selectbackground=colors["selection"], borderwidth=0)

        try:
            self.menu_bar.configure(bg=colors["panel"], fg=colors["fg"], activebackground=colors["selection"], activeforeground=colors["fg"])
            for index in range(self.menu_bar.index("end") + 1):
                submenu_name = self.menu_bar.entrycget(index, "menu")
                if submenu_name:
                    submenu = self.nametowidget(submenu_name)
                    if isinstance(submenu, tk.Menu):
                        submenu.configure(bg=colors["panel"], fg=colors["fg"], activebackground=colors["selection"], activeforeground=colors["fg"])
        except Exception:
            pass

    def _settings_from_controls(self) -> Settings:
        settings = Settings.from_env()
        settings.mode = MODE_LABELS[self.mode_var.get()]
        settings.universe_size = max(3, int(self.universe_var.get()))
        settings.top_n = max(1, min(int(self.top_var.get()), settings.universe_size))
        settings.scan_interval_seconds = max(30, int(self.interval_var.get()))
        settings.use_order_book = self.order_book_var.get()
        settings.webull_app_key = self.settings.webull_app_key
        settings.webull_app_secret = self.settings.webull_app_secret
        settings.webull_region = self.settings.webull_region
        settings.webull_api_endpoint = self.settings.webull_api_endpoint
        settings.webull_token_dir = self.settings.webull_token_dir
        settings.min_price = self.settings.min_price
        settings.max_price = self.settings.max_price
        settings.min_dollar_volume_5m = self.settings.min_dollar_volume_5m
        settings.database_path = self.settings.database_path
        settings.latest_csv_path = self.settings.latest_csv_path
        settings.model_path = self.settings.model_path
        settings.learning_enabled = self.settings.learning_enabled
        settings.learning_horizon_hours = self.settings.learning_horizon_hours
        settings.learning_thresholds = self.settings.learning_thresholds
        settings.learning_primary_threshold = self.settings.learning_primary_threshold
        settings.learning_track_top_n = self.settings.learning_track_top_n
        settings.learning_event_gap_minutes = self.settings.learning_event_gap_minutes
        settings.learning_finalize_per_scan = self.settings.learning_finalize_per_scan
        settings.learning_refresh_symbols_per_scan = self.settings.learning_refresh_symbols_per_scan
        settings.learning_min_bars_per_event = self.settings.learning_min_bars_per_event
        settings.learning_retrain_hours = self.settings.learning_retrain_hours
        settings.learning_min_rows = self.settings.learning_min_rows
        settings.learning_min_positives = self.settings.learning_min_positives
        settings.learning_min_new_labels = self.settings.learning_min_new_labels
        settings.learning_min_ap_improvement = self.settings.learning_min_ap_improvement
        settings.learning_max_brier_degradation = self.settings.learning_max_brier_degradation
        settings.learning_min_lift_over_base = self.settings.learning_min_lift_over_base
        settings.ensure_directories()
        return settings

    @staticmethod
    def _provider(settings: Settings):
        if settings.mode == "mock":
            return MockMarketDataProvider()
        if settings.mode == "webull":
            return WebullMarketDataProvider(settings)
        raise ValueError(f"Unbekannter Datenmodus: {settings.mode}")

    def start_single_scan(self) -> None:
        if self.scan_in_progress:
            return
        try:
            settings = self._settings_from_controls()
        except Exception as exc:
            messagebox.showerror("Ungültige Eingabe", str(exc), parent=self)
            return

        self.scan_in_progress = True
        self.status_var.set("Scan läuft …")
        self.countdown_var.set("")
        self._set_scan_controls(False)
        self._append_log(f"Scan gestartet: {self.mode_var.get()}, Universum {settings.universe_size}")
        thread = threading.Thread(target=self._scan_worker, args=(settings,), daemon=True)
        thread.start()

    def _scan_worker(self, settings: Settings) -> None:
        try:
            provider = self._provider(settings)
            scanner = MomentumScanner(settings, provider, Storage(settings.database_path))
            result = scanner.scan_once()
            self.messages.put(("result", result))
        except Exception as exc:
            self.messages.put(("error", (str(exc), traceback.format_exc())))

    def _scan_finished(self, result) -> None:
        self.scan_in_progress = False
        self.latest_result = result
        self.consecutive_network_errors = 0
        self._set_scan_controls(True)
        now = datetime.now().strftime("%d.%m.%Y, %H:%M:%S")
        self.last_scan_var.set(f"Letzter Scan: {now} · {result.universe_count} Ausgangswerte")
        if result.errors:
            self.status_var.set(f"Fertig mit {len(result.errors)} Warnung(en)")
        elif result.candidates:
            self.status_var.set(f"Fertig – {len(result.candidates)} Kandidat(en)")
        else:
            self.status_var.set("Fertig – keine Kandidaten nach Filterung")
        self._display_result(result)
        self._load_learning_status()
        learn = getattr(result, "learning_summary", {}) or {}
        if learn:
            self._append_log(
                "Lernzyklus: "
                f"{learn.get('registered', 0)} neue Fälle, "
                f"{learn.get('finalized', 0)} ausgewertet, "
                f"{learn.get('models_accepted', 0)} Modell(e) übernommen."
            )
        self._append_log(
            f"Scan beendet: {len(result.all_observations)} analysiert, "
            f"{len(result.candidates)} Top-Kandidaten, {len(result.errors)} Fehler/Warnungen"
        )
        for error in result.errors:
            self._append_log(f"Warnung: {error}")
        if self.auto_running:
            self._schedule_next_scan()

    def _scan_failed(self, message: str, details: str) -> None:
        self.scan_in_progress = False
        self._set_scan_controls(True)
        combined = f"{message}\n{details}"
        transient = is_transient_network_error(combined)

        if transient:
            self.consecutive_network_errors += 1
            retry_seconds = max(30, int(self.settings.network_error_retry_seconds))
            self.status_var.set(
                f"Verbindung unterbrochen – neuer Versuch in {retry_seconds} Sekunden"
            )
            self._append_log(
                "Vorübergehender Netzwerkfehler: "
                f"{message}. Versuch {self.consecutive_network_errors}; "
                f"erneuter Scan in {retry_seconds} Sekunden."
            )
            self._append_log(details)
            if self.auto_running:
                self._schedule_next_scan(delay_seconds=retry_seconds)
                return
            messagebox.showwarning(
                "Verbindung vorübergehend unterbrochen",
                "Die Verbindung zu Webull wurde kurzfristig geschlossen. "
                "Die Zugangsdaten sind dadurch nicht automatisch falsch. "
                "Bitte den Scan erneut starten.\n\n"
                f"Technische Meldung:\n{message}",
                parent=self,
            )
            return

        self.status_var.set("Scan fehlgeschlagen")
        self._append_log(f"Fehler: {message}")
        self._append_log(details)
        if self.auto_running:
            self.stop_auto_scan()
        messagebox.showerror("Scan fehlgeschlagen", message, parent=self)

    def start_learning_cycle(self) -> None:
        if self.scan_in_progress:
            return
        try:
            settings = self._settings_from_controls()
        except Exception as exc:
            messagebox.showerror("Ungültige Eingabe", str(exc), parent=self)
            return
        self.scan_in_progress = True
        self.status_var.set("24-h-Lernzyklus läuft …")
        self._set_scan_controls(False)
        thread = threading.Thread(
            target=self._learning_worker, args=(settings,), daemon=True
        )
        thread.start()

    def _learning_worker(self, settings: Settings) -> None:
        try:
            scanner = MomentumScanner(
                settings, self._provider(settings), Storage(settings.database_path)
            )
            summary = scanner.run_learning_cycle(force_training=True)
            self.messages.put(("learning_result", summary))
        except Exception as exc:
            self.messages.put(("error", (str(exc), traceback.format_exc())))

    def _learning_finished(self, summary: dict) -> None:
        self.scan_in_progress = False
        self._set_scan_controls(True)
        self.status_var.set(
            f"Lernen geprüft – {summary.get('finalized', 0)} Fälle ausgewertet"
        )
        self._append_log(
            "Manueller Lernzyklus beendet: "
            f"{summary.get('finalized', 0)} ausgewertet, "
            f"{summary.get('models_accepted', 0)} übernommen, "
            f"{summary.get('models_rejected', 0)} verworfen."
        )
        for warning in summary.get("warnings", []):
            self._append_log(f"Lernhinweis: {warning}")
        self._load_learning_status()

    def _load_learning_status(self) -> None:
        if not hasattr(self, "learning_tree"):
            return
        try:
            status = self.storage.learning_status(
                self.settings.learning_horizon_hours,
                self.settings.learning_thresholds,
            )
        except Exception as exc:
            self.learning_summary_var.set(f"Lernstatus nicht verfügbar: {exc}")
            return
        counts = status.get("counts", {})
        self.learning_summary_var.set(
            f"Offen: {counts.get('pending', 0)} · Ausgewertet: "
            f"{counts.get('finalized', 0)} · Insgesamt: {counts.get('total', 0)}"
        )
        self._clear_tree(self.learning_tree)
        for target in status.get("targets", []):
            model_run = target.get("model")
            metrics = (model_run or {}).get("metrics", {})
            if model_run:
                model_status = "Aktiv"
                trained_at = str(model_run.get("trained_at", ""))[:19].replace("T", " ")
            else:
                model_status = "Sammelt Daten"
                trained_at = "–"
            rows = int(target.get("rows", 0))
            positives = int(target.get("positives", 0))
            base_rate = positives / rows if rows else None
            self.learning_tree.insert(
                "", "end",
                values=(
                    f"≥ +{float(target['threshold']):.0%}",
                    model_status,
                    str(rows),
                    str(positives),
                    probability_text(base_rate),
                    de_number(metrics.get("average_precision"), 4) if metrics else "–",
                    de_number(metrics.get("brier_score"), 4) if metrics else "–",
                    probability_text(metrics.get("top_decile_precision")) if metrics else "–",
                    probability_text(metrics.get("daily_precision_at_3")) if metrics else "–",
                    metrics.get("model_name", "–") if metrics else "–",
                    trained_at,
                ),
            )

    def start_auto_scan(self) -> None:
        if self.auto_running:
            return
        self.auto_running = True
        self.auto_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append_log("Automatischer Scan wurde gestartet.")
        if not self.scan_in_progress:
            self.start_single_scan()

    def stop_auto_scan(self) -> None:
        self.auto_running = False
        if self.auto_after_id:
            self.after_cancel(self.auto_after_id)
            self.auto_after_id = None
        self.auto_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.countdown_var.set("")
        self._append_log("Automatischer Scan wurde gestoppt.")

    def _schedule_next_scan(self, delay_seconds: int | None = None) -> None:
        if delay_seconds is None:
            try:
                interval = max(30, int(self.interval_var.get()))
            except Exception:
                interval = 300
        else:
            interval = max(30, int(delay_seconds))
        self._countdown_remaining = interval
        self._update_countdown()

    def _update_countdown(self) -> None:
        if not self.auto_running:
            return
        if self._countdown_remaining <= 0:
            self.countdown_var.set("")
            self.auto_after_id = None
            self.start_single_scan()
            return
        minutes, seconds = divmod(self._countdown_remaining, 60)
        self.countdown_var.set(f"Nächster Scan in {minutes:02d}:{seconds:02d}")
        self._countdown_remaining -= 1
        self.auto_after_id = self.after(1000, self._update_countdown)

    def _set_scan_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.scan_button.configure(state=state)
        self.mode_combo.configure(state="readonly" if enabled else "disabled")
        self.universe_spin.configure(state=state)
        self.top_spin.configure(state=state)
        self.interval_spin.configure(state=state)
        self.learn_button.configure(state=state)
        if enabled and not self.auto_running:
            self.auto_button.configure(state="normal")
        else:
            self.auto_button.configure(state="disabled")

    def _display_result(self, result) -> None:
        self._clear_tree(self.top_tree)
        self._clear_tree(self.all_tree)
        self._candidate_lookup: dict[str, object] = {}
        symbols = [candidate.symbol for candidate in result.all_observations]
        self._recommendation_lookup = self.storage.recommendations_for(symbols)
        top_symbols = {candidate.symbol for candidate in result.candidates}
        for candidate in result.all_observations:
            self._candidate_lookup[candidate.symbol] = candidate
            values = self._candidate_values(candidate)
            self.all_tree.insert("", "end", iid=f"all-{candidate.symbol}", values=values + (("Ja" if candidate.symbol in top_symbols else "Nein"),))
        for candidate in result.candidates:
            self.top_tree.insert("", "end", iid=f"top-{candidate.symbol}", values=self._candidate_values(candidate))
        if result.candidates:
            first = result.candidates[0]
            self.top_tree.selection_set(f"top-{first.symbol}")
            self.top_tree.focus(f"top-{first.symbol}")
            self._show_candidate_details(first)

    def _candidate_values(self, candidate) -> tuple[str, ...]:
        reasons = " · ".join(candidate.reasons) if candidate.reasons else "Keine Begründung verfügbar"
        recommendation = getattr(self, "_recommendation_lookup", {}).get(
            str(candidate.symbol).upper(), {}
        )
        first_seen = recommendation.get("first_seen_at")
        assessment_time = first_seen or getattr(candidate, "timestamp", None)
        phase = _market_phase(assessment_time)
        assessment = purchase_assessment(
            candidate.signal_score,
            candidate.risk_score,
            candidate.model_probability,
            candidate.features,
            age_minutes=signal_age_minutes(first_seen) if first_seen else 0.0,
            phase=phase,
        )
        values: list[str] = [
            str(candidate.rank),
            candidate.symbol,
            _format_recommendation_time(first_seen),
            _format_recommendation_time(first_seen, eastern=True),
            _signal_age(first_seen),
            _market_phase(first_seen),
            de_number(candidate.price, 4),
            de_number(candidate.signal_score, 2),
            de_number(candidate.risk_score, 2),
            de_number(combined_score(candidate.signal_score, candidate.risk_score), 2),
            de_number(ranking_score(
                candidate.signal_score, candidate.risk_score, candidate.model_probability
            ), 2),
            de_number(assessment.score, 2),
            assessment.quality,
        ]
        probabilities = getattr(candidate, "model_probabilities", {}) or {}
        for _, threshold in self._probability_columns():
            values.append(probability_text(probabilities.get(f"{threshold:.6f}")))
        values.append(reasons)
        return tuple(values)

    def _candidate_selected(self, event) -> None:
        tree = event.widget
        selection = tree.selection()
        if not selection:
            return
        values = tree.item(selection[0], "values")
        if len(values) < 2:
            return
        symbol = str(values[1])
        candidate = getattr(self, "_candidate_lookup", {}).get(symbol)
        if candidate:
            self._show_candidate_details(candidate)

    def _show_candidate_details(self, candidate) -> None:
        self.detail_symbol_var.set(candidate.symbol)
        self._clear_tree(self.detail_tree)
        recommendation = getattr(self, "_recommendation_lookup", {}).get(
            str(candidate.symbol).upper(), {}
        )
        first_seen = recommendation.get("first_seen_at")
        if recommendation:
            meta_rows = [
                ("Erste Empfehlung (lokal)", _format_recommendation_time(first_seen)),
                ("Erste Empfehlung (ET)", _format_recommendation_time(first_seen, eastern=True)),
                ("Marktphase bei Empfehlung", _market_phase(first_seen)),
                ("Erkennungspreis", f"{de_number(recommendation.get('first_seen_price'), 4)} USD"),
                ("Letztes Signal (lokal)", _format_recommendation_time(recommendation.get("last_seen_at"))),
                ("Signal-Alter", _signal_age(first_seen)),
                ("In Top-Kandidaten gesehen", str(recommendation.get("seen_count", 1))),
            ]
            for label, value in meta_rows:
                self.detail_tree.insert("", "end", values=(label, value))
            self.detail_tree.insert("", "end", values=("────────", "────────"))
        assessment_time = first_seen or getattr(candidate, "timestamp", None)
        assessment = purchase_assessment(
            candidate.signal_score,
            candidate.risk_score,
            candidate.model_probability,
            candidate.features,
            age_minutes=signal_age_minutes(first_seen) if first_seen else 0.0,
            phase=_market_phase(assessment_time),
        )
        score_rows = [
            ("Signal-Score", de_number(candidate.signal_score, 2)),
            ("Risiko-Score", de_number(candidate.risk_score, 2)),
            ("Gesamt-Score", de_number(combined_score(candidate.signal_score, candidate.risk_score), 2)),
            ("Ranking-Score", de_number(ranking_score(
                candidate.signal_score, candidate.risk_score, candidate.model_probability
            ), 2)),
            ("Kauf-Score (Forschung)", de_number(assessment.score, 2)),
            ("Kauf-Qualität", assessment.quality),
            ("Kauf-Filter", "Bestanden" if assessment.eligible else ("; ".join(assessment.blockers) or "Beobachten")),
            ("Signal-Frische", de_number(assessment.freshness, 2)),
            ("Ausführungsqualität", de_number(assessment.execution_quality, 2)),
            ("Top-Empfehlung zulässig", "Ja" if is_top_eligible(candidate.signal_score) else f"Nein (< {MIN_TOP_SIGNAL_SCORE:.0f})"),
        ]
        for label, value in score_rows:
            self.detail_tree.insert("", "end", values=(label, value))
        self.detail_tree.insert("", "end", values=("────────", "────────"))
        features = sorted(candidate.features.items(), key=lambda item: feature_label(item[0]))
        for name, value in features:
            self.detail_tree.insert("", "end", values=(feature_label(name), format_feature(name, value)))

    def _load_latest_from_storage(self) -> None:
        try:
            frame = self.storage.latest_frame()
        except Exception as exc:
            self._append_log(f"Gespeicherte Daten konnten nicht geladen werden: {exc}")
            return
        if frame.empty:
            return

        class RowCandidate:
            pass

        candidates = []
        all_observations = []
        for row in frame.to_dict("records"):
            candidate = RowCandidate()
            candidate.rank = int(row["rank"])
            candidate.symbol = row["symbol"]
            candidate.timestamp = row.get("timestamp")
            candidate.price = float(row["price"])
            candidate.signal_score = float(row["signal_score"])
            candidate.risk_score = float(row["risk_score"])
            candidate.model_probability = row.get("model_probability")
            if candidate.model_probability != candidate.model_probability:
                candidate.model_probability = None
            candidate.model_probabilities = json.loads(
                row.get("model_probabilities_json") or "{}"
            )
            candidate.features = json.loads(row.get("features_json") or "{}")
            candidate.reasons = json.loads(row.get("reasons_json") or "[]")
            all_observations.append(candidate)
            if int(row.get("selected_top", 0)) == 1:
                candidates.append(candidate)

        class StoredResult:
            pass

        result = StoredResult()
        result.candidates = candidates
        result.all_observations = all_observations
        result.errors = []
        result.universe_count = len(all_observations)
        self._display_result(result)
        self.status_var.set("Zuletzt gespeicherte Daten geladen")
        self._load_learning_status()

    def _process_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "result":
                    self._scan_finished(payload)
                elif kind == "error":
                    message, details = payload
                    self._scan_failed(message, details)
                elif kind == "learning_result":
                    self._learning_finished(payload)
                elif kind == "log":
                    self._append_log(str(payload))
        except queue.Empty:
            pass
        self.after(100, self._process_messages)
        self.orderflow_after_id = self.after(500, self._refresh_orderflow_status)

    def _append_log(self, text: str) -> None:
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    @staticmethod
    def _clear_tree(tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _open_latest_csv(self) -> None:
        self._open_path(self.settings.latest_csv_path)

    def _open_data_folder(self) -> None:
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._open_path(self.settings.database_path.parent)

    def _open_env_file(self) -> None:
        env_path = self.project_dir / ".env"
        if not env_path.exists():
            example = self.project_dir / ".env.example"
            if example.exists():
                env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                env_path.write_text("SCANNER_MODE=mock\n", encoding="utf-8")
        if os.name == "nt":
            subprocess.Popen(["notepad.exe", str(env_path)])
        else:
            self._open_path(env_path)

    def _open_path(self, path: Path) -> None:
        path = path.resolve()
        if not path.exists():
            messagebox.showinfo("Nicht vorhanden", f"Die Datei oder der Ordner existiert noch nicht:\n{path}", parent=self)
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            webbrowser.open(path.as_uri())

    def _open_settings_dialog(self) -> None:
        dialog = Toplevel(self)
        dialog.title("Einstellungen")
        dialog.geometry("700x760")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Webull und Analysefilter", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))

        key_var = StringVar(value=self.settings.webull_app_key)
        secret_var = StringVar(value=self.settings.webull_app_secret)
        region_var = StringVar(value=self.settings.webull_region)
        endpoint_var = StringVar(value=self.settings.webull_api_endpoint)
        min_price_var = StringVar(value=str(self.settings.min_price))
        max_price_var = StringVar(value=str(self.settings.max_price))
        min_volume_var = StringVar(value=str(self.settings.min_dollar_volume_5m))
        order_book_var = BooleanVar(value=self.order_book_var.get())
        learning_enabled_var = BooleanVar(value=self.settings.learning_enabled)
        horizon_var = StringVar(value=str(self.settings.learning_horizon_hours))
        track_var = StringVar(value=str(self.settings.learning_track_top_n))

        fields = [
            ("Webull App Key", key_var, False),
            ("Webull App Secret", secret_var, True),
            ("Region", region_var, False),
            ("API-Endpunkt", endpoint_var, False),
            ("Mindestkurs in USD", min_price_var, False),
            ("Höchstkurs in USD", max_price_var, False),
            ("Mindest-Dollarvolumen in 5 Min.", min_volume_var, False),
        ]
        for row, (label, variable, secret) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=7, padx=(0, 12))
            entry = ttk.Entry(frame, textvariable=variable, width=48, show="•" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=7)

        ttk.Checkbutton(
            frame, text="Orderbuchdaten verwenden, wenn verfügbar",
            variable=order_book_var
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 8))

        ttk.Separator(frame, orient="horizontal").grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=10
        )
        ttk.Checkbutton(
            frame, text="Automatischen Lernkreislauf aktivieren",
            variable=learning_enabled_var
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(4, 8))
        ttk.Label(frame, text="Prognosehorizont in Stunden").grid(
            row=11, column=0, sticky="w", pady=7, padx=(0, 12)
        )
        ttk.Entry(frame, textvariable=horizon_var, width=48).grid(
            row=11, column=1, sticky="ew", pady=7
        )
        ttk.Label(frame, text="Pro Scan zu verfolgende Fälle").grid(
            row=12, column=0, sticky="w", pady=7, padx=(0, 12)
        )
        ttk.Entry(frame, textvariable=track_var, width=48).grid(
            row=12, column=1, sticky="ew", pady=7
        )
        ttk.Label(
            frame,
            text=(
                "Ziele: +10 %, +20 %, +50 % und +100 % innerhalb von 24 Stunden. "
                "Zugangsdaten werden derzeit lokal in .env gespeichert. Das Programm führt keine Orders aus."
            ),
            style="Muted.TLabel",
            wraplength=640,
        ).grid(row=13, column=0, columnspan=2, sticky="w", pady=(8, 18))

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=14, column=0, columnspan=2, sticky="e")

        def save() -> None:
            try:
                min_price = float(min_price_var.get().replace(",", "."))
                max_price = float(max_price_var.get().replace(",", "."))
                min_volume = float(min_volume_var.get().replace(",", "."))
                horizon = float(horizon_var.get().replace(",", "."))
                track_top_n = int(track_var.get())
                if min_price < 0 or max_price <= min_price or min_volume < 0:
                    raise ValueError("Bitte gültige Filterwerte eingeben.")
                if not 1 <= horizon <= 168:
                    raise ValueError("Der Prognosehorizont muss zwischen 1 und 168 Stunden liegen.")
                if track_top_n < 3:
                    raise ValueError("Es müssen mindestens 3 Fälle pro Scan verfolgt werden.")
            except ValueError as exc:
                messagebox.showerror("Ungültige Eingabe", str(exc), parent=dialog)
                return

            self.settings.webull_app_key = key_var.get().strip()
            self.settings.webull_app_secret = secret_var.get().strip()
            self.settings.webull_region = region_var.get().strip() or "us"
            self.settings.webull_api_endpoint = endpoint_var.get().strip() or "api.webull.com"
            self.settings.min_price = min_price
            self.settings.max_price = max_price
            self.settings.min_dollar_volume_5m = min_volume
            self.settings.use_order_book = order_book_var.get()
            self.settings.learning_enabled = learning_enabled_var.get()
            self.settings.learning_horizon_hours = horizon
            self.settings.learning_track_top_n = track_top_n
            self.order_book_var.set(order_book_var.get())
            self._write_env()
            self._append_log("Einstellungen wurden gespeichert.")
            dialog.destroy()

        ttk.Button(button_frame, text="Abbrechen", command=dialog.destroy).pack(side="left", padx=8)
        ttk.Button(button_frame, text="Speichern", style="Accent.TButton", command=save).pack(side="left")
        frame.columnconfigure(1, weight=1)
        self._apply_theme()

    def _write_env(self) -> None:
        env_path = self.project_dir / ".env"
        values = {
            "SCANNER_MODE": MODE_LABELS[self.mode_var.get()],
            "WEBULL_APP_KEY": self.settings.webull_app_key,
            "WEBULL_APP_SECRET": self.settings.webull_app_secret,
            "WEBULL_REGION": self.settings.webull_region,
            "WEBULL_API_ENDPOINT": self.settings.webull_api_endpoint,
            "UNIVERSE_SIZE": str(self.universe_var.get()),
            "TOP_N": str(self.top_var.get()),
            "SCAN_INTERVAL_SECONDS": str(self.interval_var.get()),
            "USE_ORDER_BOOK": "true" if self.order_book_var.get() else "false",
            "MIN_PRICE": str(self.settings.min_price),
            "MAX_PRICE": str(self.settings.max_price),
            "MIN_DOLLAR_VOLUME_5M": str(self.settings.min_dollar_volume_5m),
            "LEARNING_ENABLED": "true" if self.settings.learning_enabled else "false",
            "LEARNING_HORIZON_HOURS": str(self.settings.learning_horizon_hours),
            "LEARNING_THRESHOLDS": ",".join(str(v) for v in self.settings.learning_thresholds),
            "LEARNING_PRIMARY_THRESHOLD": str(self.settings.learning_primary_threshold),
            "LEARNING_TRACK_TOP_N": str(self.settings.learning_track_top_n),
            "LEARNING_EVENT_GAP_MINUTES": str(self.settings.learning_event_gap_minutes),
            "LEARNING_FINALIZE_PER_SCAN": str(self.settings.learning_finalize_per_scan),
            "LEARNING_REFRESH_SYMBOLS_PER_SCAN": str(self.settings.learning_refresh_symbols_per_scan),
            "LEARNING_MIN_BARS_PER_EVENT": str(self.settings.learning_min_bars_per_event),
            "LEARNING_RETRAIN_HOURS": str(self.settings.learning_retrain_hours),
            "LEARNING_MIN_ROWS": str(self.settings.learning_min_rows),
            "LEARNING_MIN_POSITIVES": str(self.settings.learning_min_positives),
            "LEARNING_MIN_NEW_LABELS": str(self.settings.learning_min_new_labels),
            "LEARNING_MIN_AP_IMPROVEMENT": str(self.settings.learning_min_ap_improvement),
            "LEARNING_MAX_BRIER_DEGRADATION": str(self.settings.learning_max_brier_degradation),
            "LEARNING_MIN_LIFT_OVER_BASE": str(self.settings.learning_min_lift_over_base),
        }
        existing: list[str] = []
        if env_path.exists():
            existing = env_path.read_text(encoding="utf-8").splitlines()
        output: list[str] = []
        handled: set[str] = set()
        for line in existing:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                output.append(line)
                continue
            key = line.split("=", 1)[0].strip()
            if key in values:
                output.append(f"{key}={values[key]}")
                handled.add(key)
            else:
                output.append(line)
        for key, value in values.items():
            if key not in handled:
                output.append(f"{key}={value}")
        env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")

    def _show_help(self) -> None:
        messagebox.showinfo(
            "Hinweise",
            "1. Testdaten auswählen und 'Jetzt scannen' drücken.\n\n"
            "2. Für Webull die Zugangsdaten unter Einstellungen eintragen.\n\n"
            "3. 'Automatik starten' führt den Scan im gewählten Intervall aus.\n\n"
            "4. Ein Doppelklick auf eine Aktie öffnet die Merkmalsansicht.\n\n"
            "Der Scanner analysiert nur und führt keine Käufe oder Verkäufe aus.",
            parent=self,
        )

    def _show_about(self) -> None:
        messagebox.showinfo(
            "Über den US Momentum Scanner",
            f"US Momentum Scanner – Windows-Oberfläche, Version {APP_VERSION}\n\n"
            "Dunkelmodus, deutsche Anzeigen, prospektive Top-Kandidaten und lokale Speicherung.\n\n"
            "Signal-, Risiko-, Gesamt- und Ranking-Score sind Forschungswerte und keine Anlageberatung.\n\n"
            "Gesamt-Score = Signal-Score × (1 - 0,5 × Risiko-Score / 100).\n"
            f"Top-Empfehlungen benötigen mindestens Signal-Score {MIN_TOP_SIGNAL_SCORE:.0f}.\n"
            "Ranking-Score = 50 % Gesamt-Score + 30 % Modellwahrscheinlichkeit + 20 % Signal-Score.\n"
            "Ohne Modell: 70 % Gesamt-Score + 30 % Signal-Score.",
            parent=self,
        )

    def _open_orderflow_monitor(self) -> None:
        try:
            started = self.orderflow_controller.start()
        except OrderflowLaunchError as exc:
            logger.exception("Orderflow-Monitor konnte nicht gestartet werden")
            messagebox.showerror("Orderflow", str(exc), parent=self)
            self._refresh_orderflow_status()
            return

        if not started:
            messagebox.showinfo(
                "Orderflow",
                "Der von dieser Scanner-Sitzung gestartete Orderflow-Monitor läuft bereits.",
                parent=self,
            )
        self._refresh_orderflow_status()

    def _stop_orderflow_monitor(self) -> None:
        if not self.orderflow_controller.is_running:
            self._refresh_orderflow_status()
            return
        if not messagebox.askyesno(
            "Orderflow beenden",
            "Den separat gestarteten Orderflow-Monitor jetzt beenden?",
            parent=self,
        ):
            return
        try:
            self.orderflow_controller.stop()
        except Exception as exc:
            logger.exception("Orderflow-Monitor konnte nicht beendet werden")
            messagebox.showerror("Orderflow", f"Beenden fehlgeschlagen: {exc}", parent=self)
        self._refresh_orderflow_status()

    def _refresh_orderflow_status(self) -> None:
        existing_after = getattr(self, "orderflow_after_id", None)
        if existing_after:
            try:
                self.after_cancel(existing_after)
            except Exception:
                pass
        self.orderflow_after_id = None

        running = self.orderflow_controller.is_running
        if running:
            self.orderflow_status_var.set("Orderflow: LÄUFT")
            self.orderflow_open_button.configure(state="disabled")
            self.orderflow_stop_button.configure(state="normal")
        else:
            return_code = self.orderflow_controller.return_code
            if return_code not in (None, 0):
                self.orderflow_status_var.set(f"Orderflow: BEENDET ({return_code})")
            else:
                self.orderflow_status_var.set("Orderflow: AUS")
            self.orderflow_open_button.configure(state="normal")
            self.orderflow_stop_button.configure(state="disabled")

        if self.winfo_exists():
            self.orderflow_after_id = self.after(1000, self._refresh_orderflow_status)

    def _on_close(self) -> None:
        self.stop_auto_scan()
        if getattr(self, "orderflow_after_id", None):
            try:
                self.after_cancel(self.orderflow_after_id)
            except Exception:
                pass
        # Der Orderflow-Monitor bleibt absichtlich als unabhängiger Prozess geöffnet.
        logging.getLogger().removeHandler(self._log_handler)
        self.destroy()


def main() -> None:
    app = MomentumScannerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
