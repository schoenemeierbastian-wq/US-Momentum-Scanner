from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

COLORS = {
    "bg": "#071321",
    "panel": "#0d1d30",
    "panel_alt": "#10243a",
    "header": "#030b15",
    "border": "#29435e",
    "text": "#f2f6fb",
    "muted": "#9db0c7",
    "gold": "#efb83f",
    "gold_dark": "#8a5712",
    "blue": "#1e9ee8",
    "green": "#48d168",
    "red": "#ff5b57",
    "orange": "#ffad36",
    "selection": "#116a97",
    "entry": "#081727",
}


def apply_dark_theme(root: tk.Misc) -> ttk.Style:
    """Wendet eine einheitliche dunkle Tk/ttk-Optik an."""
    root.configure(bg=COLORS["bg"])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=COLORS["bg"], foreground=COLORS["text"], font=("Segoe UI", 9))
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("Panel.TFrame", background=COLORS["panel"])
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", foreground=COLORS["muted"])
    style.configure("Gold.TLabel", foreground=COLORS["gold"], font=("Segoe UI Semibold", 10))
    style.configure("Success.TLabel", foreground=COLORS["green"], font=("Segoe UI Semibold", 10))
    style.configure("Warning.TLabel", foreground=COLORS["orange"], font=("Segoe UI Semibold", 10))
    style.configure("Error.TLabel", foreground=COLORS["red"], font=("Segoe UI Semibold", 10))

    style.configure(
        "TButton",
        background=COLORS["panel_alt"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
        padding=(12, 7),
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("active", "#183652"), ("disabled", "#172536")],
        foreground=[("disabled", "#607087")],
    )
    style.configure("Accent.TButton", background="#94600f", foreground="#ffffff", padding=(14, 8))
    style.map("Accent.TButton", background=[("active", "#b87812"), ("disabled", "#4c3a21")])
    style.configure("Success.TButton", background="#145d31", foreground="#ffffff", padding=(14, 8))
    style.map("Success.TButton", background=[("active", "#18723b"), ("disabled", "#243a2e")])
    style.configure("Danger.TButton", background="#8f2428", foreground="#ffffff", padding=(14, 8))
    style.map("Danger.TButton", background=[("active", "#aa2d32"), ("disabled", "#432a2d")])

    style.configure(
        "TEntry",
        fieldbackground=COLORS["entry"],
        foreground=COLORS["text"],
        insertcolor=COLORS["text"],
        bordercolor=COLORS["border"],
        padding=5,
    )
    style.configure(
        "TSpinbox",
        fieldbackground=COLORS["entry"],
        foreground=COLORS["text"],
        insertcolor=COLORS["text"],
        bordercolor=COLORS["border"],
        arrowcolor=COLORS["text"],
        padding=4,
    )
    style.map("TSpinbox", fieldbackground=[("readonly", COLORS["entry"])])
    style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["text"], focuscolor=COLORS["bg"])
    style.map("TCheckbutton", background=[("active", COLORS["bg"])])

    style.configure(
        "TLabelframe",
        background=COLORS["panel"],
        bordercolor=COLORS["border"],
        relief="solid",
        borderwidth=1,
    )
    style.configure(
        "TLabelframe.Label",
        background=COLORS["panel"],
        foreground=COLORS["gold"],
        font=("Segoe UI Semibold", 9),
    )

    style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
    style.configure(
        "TNotebook.Tab",
        background=COLORS["panel_alt"],
        foreground=COLORS["muted"],
        padding=(16, 8),
        borderwidth=1,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", COLORS["panel"])],
        foreground=[("selected", COLORS["text"])],
    )

    style.configure(
        "Treeview",
        background=COLORS["panel"],
        fieldbackground=COLORS["panel"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        rowheight=27,
    )
    style.map("Treeview", background=[("selected", COLORS["selection"])], foreground=[("selected", "#ffffff")])
    style.configure(
        "Treeview.Heading",
        background=COLORS["panel_alt"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        font=("Segoe UI Semibold", 9),
        padding=(6, 7),
    )
    style.map("Treeview.Heading", background=[("active", "#183652")])

    style.configure("Vertical.TScrollbar", background=COLORS["panel_alt"], troughcolor=COLORS["bg"], arrowcolor=COLORS["text"])
    style.configure("Horizontal.TScrollbar", background=COLORS["panel_alt"], troughcolor=COLORS["bg"], arrowcolor=COLORS["text"])
    return style


def add_branded_header(
    parent: tk.Misc,
    *,
    title: str,
    subtitle: str,
    version: str,
    safety_text: str,
) -> tk.Frame:
    """Kopfbereich mit echtem BS-Emblem und getrennt gerendertem Programmnamen."""
    outer = tk.Frame(parent, bg=COLORS["header"], highlightbackground=COLORS["border"], highlightthickness=1)
    outer.pack(fill="x", padx=10, pady=(10, 8))

    left = tk.Frame(outer, bg=COLORS["header"])
    left.pack(side="left", fill="x", expand=True, padx=14, pady=10)

    image_path = Path(__file__).resolve().parent / "assets" / "bs_emblem.png"
    try:
        image = tk.PhotoImage(file=str(image_path))
        emblem = tk.Label(left, image=image, bg=COLORS["header"], borderwidth=0)
        emblem.pack(side="left", padx=(0, 16))
        images = getattr(parent, "_brand_images", [])
        images.append(image)
        setattr(parent, "_brand_images", images)
    except (tk.TclError, OSError):
        fallback = tk.Label(
            left,
            text="BS",
            bg=COLORS["header"],
            fg=COLORS["gold"],
            font=("Georgia", 28, "bold"),
            padx=18,
            pady=18,
            highlightbackground=COLORS["gold_dark"],
            highlightthickness=1,
        )
        fallback.pack(side="left", padx=(0, 16))

    text_box = tk.Frame(left, bg=COLORS["header"])
    text_box.pack(side="left", fill="x", expand=True)
    tk.Label(
        text_box,
        text=title,
        bg=COLORS["header"],
        fg=COLORS["gold"],
        font=("Georgia", 29, "bold"),
        anchor="w",
    ).pack(fill="x", anchor="w")
    tk.Label(
        text_box,
        text=subtitle,
        bg=COLORS["header"],
        fg=COLORS["muted"],
        font=("Segoe UI", 9),
        anchor="w",
    ).pack(fill="x", anchor="w", pady=(4, 0))

    right = tk.Frame(outer, bg=COLORS["header"])
    right.pack(side="right", padx=16, pady=12)
    tk.Label(
        right,
        text=safety_text,
        bg=COLORS["header"],
        fg=COLORS["orange"],
        font=("Segoe UI Semibold", 9),
    ).pack(anchor="e")
    tk.Label(
        right,
        text=f"Version {version}",
        bg=COLORS["header"],
        fg=COLORS["muted"],
        font=("Segoe UI", 8),
    ).pack(anchor="e", pady=(7, 0))
    return outer


def make_metric_card(parent: tk.Misc, title: str, value_var: tk.StringVar, *, value_color: str | None = None) -> tk.Frame:
    frame = tk.Frame(parent, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1)
    tk.Label(frame, text=title.upper(), bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI Semibold", 8)).pack(
        anchor="w", padx=12, pady=(9, 1)
    )
    tk.Label(
        frame,
        textvariable=value_var,
        bg=COLORS["panel"],
        fg=value_color or COLORS["text"],
        font=("Segoe UI Semibold", 18),
    ).pack(anchor="w", padx=12, pady=(0, 10))
    return frame
