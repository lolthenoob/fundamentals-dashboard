"""
ticker_picker.py
────────────────
Drop-in ticker selection GUI for the IT Dashboard.
Returns (selected_tickers, years_back, force_refresh, do_export) tuple.

Bulk entry: typing comma-separated symbols (e.g. aapl,msft,xle) switches
the search box into bulk mode — Yahoo suggestions are replaced with a
preview list that looks identical to the DB rows. Each row is individually
toggleable. "Add All" ticks the checked ones and clears the box.
"""

import os
import re
import sqlite3
import tkinter as tk
from tkinter import ttk, font as tkfont
import urllib.request
import urllib.parse
import json
import threading


CLR_ACCENT  = "#00A4EF"
CLR_BG      = "#F7F9FC"
CLR_ROW_A   = "#FFFFFF"
CLR_ROW_B   = "#EFF4FA"
CLR_TEXT    = "#1A1A2E"
CLR_SUBTEXT = "#555577"
CLR_BTN_FG  = "#FFFFFF"

TICK_FONT_SIZE = 14
ROW_PADY       = 0
ROW_PADX       = 8
WINDOW_WIDTH   = 1600
WINDOW_HEIGHT  = 0.2
WINDOW_X       = 50
WINDOW_Y       = 0
YEARS_DEFAULT  = 5

MONO_FONT_SIZE = 12
BOLD_FONT_SIZE = 12
HDR_BOLD_FONT_SIZE = 14
HDR_SUB_FONT_SIZE = 14
SEL_FONT_SIZE = 12

# Maximum symbol length — anything longer is likely a paste artefact
_SYM_MAX_LEN = 6
_SYM_RE      = re.compile(r'^[A-Z0-9.\-]{1,6}$')


# ── Watchlist persistence ─────────────────────────────────────────────────────

class WatchlistManager:
    """
    Loads and saves named ticker groups to tickers/watchlists.json.

    File format:
        {
            "Semiconductors": ["NVDA", "AMD", "QCOM", "AVGO", "TXN"],
            "Dividend growers": ["MSFT", "AAPL", "JNJ", "PG"],
            "__order__": ["Dividend growers", "Semiconductors", ...]
        }

    Keys are display names; values are lists of uppercase ticker symbols.
    "__order__" is a reserved key storing the user-defined display order.
    The bar always shows the first 3 entries from __order__.
    The file is written atomically (temp file + rename) so a crash during
    save can't corrupt existing data.
    """

    DEFAULT_WATCHLISTS = {
        "Semiconductors": ["NVDA", "AMD", "QCOM", "AVGO", "TXN"],
        "Big Tech": ["MSFT", "AAPL", "GOOGL", "META", "AMZN"],
        "Dividend growers": ["MSFT", "AAPL", "JNJ", "PG", "KO"],
        "ETF sampler": ["SPY", "QQQ", "VTI", "SCHD", "VYM"],
    }
    _INTERNAL = {"__order__", "__pinned__"}   # keys never shown as watchlist names

    def __init__(self, db_path: str):
        self._path = os.path.join(os.path.dirname(db_path), "watchlists.json")
        self._data: dict[str, list[str]] = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    self._data = {}
                    for k, v in raw.items():
                        if not isinstance(v, list):
                            continue
                        if k in self._INTERNAL:
                            # Internal keys store names/strings verbatim — no uppercasing
                            self._data[k] = [str(s) for s in v if isinstance(s, str)]
                        else:
                            # Watchlist entries store ticker symbols — uppercase those
                            self._data[k] = [str(s).upper() for s in v if isinstance(s, str)]
                    self._repair_order()
                    return
            except Exception:
                pass
        # First run — seed with defaults and save
        self._data = dict(self.DEFAULT_WATCHLISTS)
        self._repair_order()
        self._save()

    def _repair_order(self):
        """Ensure __order__ exists and contains exactly the current watchlist names."""
        existing = [n for n in self._data.get("__order__", [])
                    if n in self._data and n not in self._INTERNAL]
        all_names = [k for k in self._data if k not in self._INTERNAL]
        # Append any names missing from the stored order
        for n in all_names:
            if n not in existing:
                existing.append(n)
        self._data["__order__"] = existing

    def _save(self):
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            print(f"  [watchlist] save failed: {e}")

    # ── Public API ────────────────────────────────────────────────────────

    def names(self) -> list[str]:
        """All watchlist names in user-defined order."""
        return list(self._data.get("__order__", []))

    def get(self, name: str) -> list[str]:
        return list(self._data.get(name, []))

    def order(self) -> list[str]:
        """Same as names() — the full ordered list."""
        return self.names()

    def set_order(self, ordered: list[str]):
        """Persist a new display order. Only known watchlist names are kept."""
        known = {k for k in self._data if k not in self._INTERNAL}
        self._data["__order__"] = [n for n in ordered if n in known]
        self._save()

    def bar_names(self) -> list[str]:
        """The first 3 entries in order — always shown on the bar."""
        return self.names()[:3]

    def save(self, name: str, symbols: list[str]):
        """Create or overwrite a watchlist entry."""
        is_new = name not in self._data or name in self._INTERNAL
        self._data[name] = [s.upper() for s in symbols if s]
        if is_new:
            order = self._data.get("__order__", [])
            if name not in order:
                order.append(name)
            self._data["__order__"] = order
        self._save()

    def delete(self, name: str):
        self._data.pop(name, None)
        order = self._data.get("__order__", [])
        if name in order:
            order.remove(name)
        self._data["__order__"] = order
        self._save()

    def rename(self, old_name: str, new_name: str):
        if old_name in self._data and new_name:
            self._data[new_name] = self._data.pop(old_name)
            order = self._data.get("__order__", [])
            if old_name in order:
                order[order.index(old_name)] = new_name
            self._data["__order__"] = order
            self._save()


# ── Input helpers ─────────────────────────────────────────────────────────────

def _parse_bulk(raw: str) -> list[str]:
    """
    Split a comma-separated string into clean, deduplicated, valid ticker symbols.
    Returns an empty list if the input contains no comma (single-ticker mode).
    """
    tokens = [t.strip().upper() for t in raw.split(",")]
    seen, result = set(), []
    for t in tokens:
        if not t:
            continue                       # trailing comma / double comma
        if not _SYM_RE.match(t):
            continue                       # garbage / too long / bad chars
        if t in seen:
            continue                       # duplicate
        seen.add(t)
        result.append(t)
    return result


def pick_tickers(db_path: str, _run_state: dict = None, prefs_callback=None) -> tuple[list[str], int, bool, bool]:
    available: list[tuple[str, str]] = []

    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT symbol, name FROM tickers ORDER BY symbol"
            ).fetchall()
            conn.close()
            available = [(r[0], r[1] or r[0]) for r in rows]
        except Exception:
            pass

    if not available:
        tickers, years, refresh, export = _manual_entry_fallback()
        return tickers, years, refresh, export, None, None, None, None, None, None, False

    # ── Result holders ────────────────────────────────────────────────────
    result_tickers: list[str] = []
    result_years:   int       = YEARS_DEFAULT
    result_refresh: bool      = False
    result_export:  bool      = False

    # Must be called BEFORE tk.Tk() so tkinter captures correct DPI-aware coordinates
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except Exception:
        try:
            from ctypes import windll
            windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = tk.Tk()
    root.title("📈  Select Tickers")
    root.configure(bg=CLR_BG)
    root.resizable(True, True)

    root.update_idletasks()
    try:
        import ctypes
        work = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(work), 0)
        work_w = work.right  - work.left
        work_h = work.bottom - work.top
        work_x = work.left
        work_y = work.top
       # print(f"[DEBUG GEOM] work area: {work_w}x{work_h} at ({work_x},{work_y})")
    except Exception as e:
        # print(f"[DEBUG GEOM] WorkArea failed: {e}")
        work_w = root.winfo_screenwidth()
        work_h = root.winfo_screenheight()
        work_x = 0
        work_y = 0
        # Measure actual window decoration thickness (title bar + borders).
        # We do this by letting tk render a tiny window, then reading the
        # difference between the requested and actual outer size.
    root.geometry("100x100+0+0")
    root.update_idletasks()
    # winfo_rooty gives the top of the CLIENT area; winfo_y is the outer top.
    deco_h = root.winfo_rooty() - root.winfo_y()  # title-bar height in px
    deco_w = root.winfo_rootx() - root.winfo_x()  # border width in px

    # Safety floor in case the WM hasn't committed geometry yet
    if deco_h < 1:
        deco_h = 32
    if deco_w < 1:
        deco_w = 4

    w = min(WINDOW_WIDTH, work_w - deco_w * 2)
    h = work_h - WINDOW_Y - deco_h  # subtract real title-bar height
    x = work_x + WINDOW_X
    y = work_y + WINDOW_Y
   # print(f"[DEBUG GEOM] deco {deco_w}x{deco_h} | setting geometry {w}x{h}+{x}+{y}")
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.update_idletasks()
   # print(f"[DEBUG GEOM] actual root size: {root.winfo_width()}x{root.winfo_height()} at ({root.winfo_x()},{root.winfo_y()})")
    root.minsize(500, 400)

    mono      = tkfont.Font(family="Consolas", size=MONO_FONT_SIZE)
    bold      = tkfont.Font(family="Consolas", size=BOLD_FONT_SIZE, weight="bold")
    hdr_bold  = tkfont.Font(family="Consolas", size=HDR_BOLD_FONT_SIZE, weight="bold")
    hdr_sub   = tkfont.Font(family="Consolas", size=HDR_SUB_FONT_SIZE)
    sel_font  = tkfont.Font(family="Consolas", size=SEL_FONT_SIZE, weight="bold")
    tick_font = tkfont.Font(family="Segoe UI Symbol", size=TICK_FONT_SIZE)

    # ── Menubar ───────────────────────────────────────────────────────────
    menu_font = tkfont.Font(family="Consolas", size=BOLD_FONT_SIZE, weight="bold")
    menubar = tk.Menu(root, font=menu_font)

    file_menu = tk.Menu(menubar, tearoff=0, font=menu_font)
    file_menu.add_command(label="Exit", command=lambda: root.destroy())
    menubar.add_cascade(label="File", menu=file_menu)

    edit_menu  = tk.Menu(menubar, tearoff=0, font=menu_font)
    prefs_menu = tk.Menu(edit_menu, tearoff=0, font=menu_font)
    if prefs_callback:
        prefs_menu.add_command(
            label="Chart Preferences…",
            command=lambda: prefs_callback(root),
        )
    else:
        prefs_menu.add_command(label="Chart Preferences…", state="disabled")
    edit_menu.add_cascade(label="Preferences", menu=prefs_menu)
    menubar.add_cascade(label="Edit", menu=edit_menu)

    root.config(menu=menubar)

    # ── Watchlist manager (loads/saves tickers/watchlists.json) ──────────
    _wl = WatchlistManager(db_path)

    # ── Header ────────────────────────────────────────────────────────────
    hdr = tk.Frame(root, bg=CLR_ACCENT, pady=12)
    hdr.pack(fill="x")
    tk.Label(hdr, text="Fundamentals Dashboard",
             bg=CLR_ACCENT, fg="white", font=hdr_bold).pack()
    tk.Label(hdr, text="Choose tickers to chart",
             bg=CLR_ACCENT, fg="#D0EEFF", font=hdr_sub).pack()

    # ── Watchlist bar ─────────────────────────────────────────────────────
    # Rendered between header and summary. Preset buttons on the left,
    # Preset buttons scroll horizontally. Save / Delete are pinned right.
    # Layout: [  "Watchlists:" label | <── scrollable canvas ──> | Save | Delete  ]

    # Two-row watchlist bar
    # Row 1 (dark):   "Watchlists" label + Save + Delete  — always fully visible
    # Row 2 (darker): scrollable preset buttons, scrollbar underneath when needed

    wl_frame = tk.Frame(root, bg="#1A1A2E")
    wl_frame.pack(fill="x")

    # Forward-declared; defined after check_vars exist (needed for load/save)
    _wl_load_fn   = [None]
    _wl_save_fn   = [None]
    _wl_delete_fn = [None]

    # ── Row 1: label + controls ───────────────────────────────────────────
    wl_row1 = tk.Frame(wl_frame, bg="#1A1A2E", pady=5, padx=14)
    wl_row1.pack(fill="x")

    tk.Label(
        wl_row1, text="Watchlists",
        bg="#1A1A2E", fg="#AAAACC", font=bold,
    ).pack(side="left")

    tk.Button(
        wl_row1, text="\u2716  Delete\u2026",
        bg="#4A2020", fg="#FFAAAA",
        font=bold, relief="flat",
        padx=12, pady=4, cursor="hand2",
        activebackground="#7A1C1C",
        command=lambda: _wl_delete_fn[0](),
    ).pack(side="right", padx=(6, 0))

    tk.Button(
        wl_row1, text="\uff0b  Save current\u2026",
        bg="#10B981", fg="white",
        font=bold, relief="flat",
        padx=12, pady=4, cursor="hand2",
        activebackground="#0D9E6E",
        command=lambda: _wl_save_fn[0](),
    ).pack(side="right", padx=(6, 0))

    tk.Frame(wl_frame, bg="#2A2A4E", height=1).pack(fill="x")  # divider

    # ── Row 2: horizontally scrollable button bar + "More ▼" popup ────────
    # The bar shows ALL watchlists as buttons in user-defined order.
    # The first 3 are always visible without scrolling (top-of-order = bar slots 1-3).
    # Users can scroll the bar left/right with the mousewheel to reveal more.
    # "More ▼" opens a full searchable + reorderable popup.

    wl_row2 = tk.Frame(wl_frame, bg="#12122A")
    wl_row2.pack(fill="x")

    # "» More ▼" button — always pinned to the right
    _wl_more_btn = tk.Button(
        wl_row2, text="  ≡  All  ▼  ",
        bg="#1E1E3A", fg="#AAAACC",
        font=bold, relief="flat",
        padx=10, pady=8, cursor="hand2",
        activebackground="#2A2A4E", activeforeground="#D0EEFF",
    )
    _wl_more_btn.pack(side="right", padx=(2, 6), pady=4)

    # Scrollable canvas for the bar buttons
    _wl_bar_canvas = tk.Canvas(wl_row2, bg="#12122A", highlightthickness=0, height=44)
    _wl_bar_canvas.pack(side="left", fill="x", expand=True)

    _wl_btn_frame = tk.Frame(_wl_bar_canvas, bg="#12122A")
    _wl_bar_win   = _wl_bar_canvas.create_window((0, 0), window=_wl_btn_frame, anchor="nw")

    def _wl_bar_scroll(event):
        """Horizontal scroll of the watchlist bar on mousewheel."""
        try:
            bx = _wl_bar_canvas.winfo_rootx()
            by = _wl_bar_canvas.winfo_rooty()
            bw = _wl_bar_canvas.winfo_width()
            bh = _wl_bar_canvas.winfo_height()
            if not (bx <= event.x_root <= bx + bw and by <= event.y_root <= by + bh):
                return
        except Exception:
            return
        num = getattr(event, "num", 0)
        delta = getattr(event, "delta", 0)
        if num == 4:
            _wl_bar_canvas.xview_scroll(-1, "units")
        elif num == 5:
            _wl_bar_canvas.xview_scroll(1, "units")
        elif num == 6:
            _wl_bar_canvas.xview_scroll(-1, "units")
        elif num == 7:
            _wl_bar_canvas.xview_scroll(1, "units")
        elif delta:
            _wl_bar_canvas.xview_scroll(int(-delta / 60), "units")

    _wl_bar_canvas.bind("<MouseWheel>",  _wl_bar_scroll)
    _wl_bar_canvas.bind("<Button-4>",    _wl_bar_scroll)
    _wl_bar_canvas.bind("<Button-5>",    _wl_bar_scroll)
    # Button-6/7 are Linux horizontal scroll — skip on Windows
    try:
        _wl_bar_canvas.bind("<Button-6>", _wl_bar_scroll)
        _wl_bar_canvas.bind("<Button-7>", _wl_bar_scroll)
    except Exception:
        pass
    _wl_btn_frame.bind("<MouseWheel>",   _wl_bar_scroll)
    _wl_btn_frame.bind("<Button-4>",     _wl_bar_scroll)
    _wl_btn_frame.bind("<Button-5>",     _wl_bar_scroll)

    def _wl_bar_update_scroll(*_):
        _wl_btn_frame.update_idletasks()
        bbox = _wl_bar_canvas.bbox("all")
        if bbox:
            _wl_bar_canvas.configure(scrollregion=bbox)
        _wl_bar_canvas.itemconfig(_wl_bar_win, height=_wl_bar_canvas.winfo_height())

    _wl_btn_frame.bind("<Configure>", _wl_bar_update_scroll)
    _wl_bar_canvas.bind("<Configure>", _wl_bar_update_scroll)

    _wl_active_name = [None]
    _wl_btn_refs: dict[str, tk.Button] = {}

    def _wl_open_overflow_menu():
        """
        Full searchable + reorderable popup listing all watchlists.
        Drag-and-drop OR arrow buttons to reorder. Order persists to JSON.
        Bar always shows first 3 entries.
        """
        all_names = _wl.names()
        if not all_names:
            return

        popup = tk.Toplevel(root)
        popup.transient(root)
        popup.configure(bg="#1A1A2E")
        popup.attributes("-topmost", True)
        _popup_open[0] = True

        # ── Header ────────────────────────────────────────────────────────
        hdr_p = tk.Frame(popup, bg="#00A4EF", pady=6, padx=14)
        hdr_p.pack(fill="x")
        tk.Label(hdr_p, text="All Watchlists",
                 bg="#00A4EF", fg="white", font=bold).pack(side="left")

        def _close_popup():
            _popup_open[0] = False
            popup.destroy()

        tk.Button(hdr_p, text="✕", bg="#00A4EF", fg="white",
                  font=bold, relief="flat", cursor="hand2",
                  activebackground="#0082C8",
                  command=_close_popup).pack(side="right")

        def _toggle_maximise():
            try:
                state = popup.state()
                if state == "zoomed":
                    popup.state("normal")
                else:
                    popup.state("zoomed")
            except Exception:
                try:
                    popup.attributes("-zoomed", not popup.attributes("-zoomed"))
                except Exception:
                    pass

        tk.Button(hdr_p, text="⛶", bg="#00A4EF", fg="white",
                  font=bold, relief="flat", cursor="hand2",
                  activebackground="#0082C8",
                  command=_toggle_maximise).pack(side="right", padx=(0, 4))

        # ── Toolbar: search + reorder mode toggle ─────────────────────────
        toolbar = tk.Frame(popup, bg="#1A1A2E", pady=6, padx=10)
        toolbar.pack(fill="x")

        tk.Label(toolbar, text="🔍", bg="#1A1A2E", fg="#AAAACC",
                 font=bold).pack(side="left", padx=(0, 6))
        filter_var = tk.StringVar()
        filter_entry = tk.Entry(toolbar, textvariable=filter_var,
                                font=mono, relief="flat", bg="#2A2A4E",
                                fg="#D0EEFF", insertbackground="#D0EEFF",
                                highlightthickness=1,
                                highlightcolor=CLR_ACCENT,
                                highlightbackground="#3A3A5E")
        filter_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        filter_entry.focus_set()

        # Mode toggle: "↕ Drag" or "▲▼ Arrows"
        _reorder_mode = ["arrows"]   # "drag" or "arrows"
        mode_btn = tk.Button(toolbar, text="↕  Drag to reorder",
                             bg="#2A2A4E", fg="#AAAACC",
                             font=bold, relief="flat",
                             padx=10, pady=4, cursor="hand2",
                             activebackground="#3A3A5E")
        mode_btn.pack(side="right")

        # ── Hint label ────────────────────────────────────────────────────
        hint_var = tk.StringVar(value="▲▼ arrows to reorder  ·  top 3 always on bar")
        hint_lbl = tk.Label(popup, textvariable=hint_var,
                            bg="#0D0D22", fg="#555577", font=mono,
                            anchor="w", padx=10, pady=3)
        hint_lbl.pack(fill="x")

        # ── Scrollable list ───────────────────────────────────────────────
        list_outer_p = tk.Frame(popup, bg="#1A1A2E")
        list_outer_p.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        vsb = ttk.Scrollbar(list_outer_p, orient="vertical")
        vsb.pack(side="right", fill="y")

        list_canvas = tk.Canvas(list_outer_p, bg="#1A1A2E",
                                highlightthickness=0,
                                yscrollcommand=vsb.set)
        list_canvas.pack(side="left", fill="both", expand=True)
        vsb.config(command=list_canvas.yview)

        list_inner = tk.Frame(list_canvas, bg="#1A1A2E")
        _list_win = list_canvas.create_window((0, 0), window=list_inner, anchor="nw")

        def _update_scrollregion(*_):
            list_canvas.update_idletasks()
            bbox = list_canvas.bbox("all")
            if bbox:
                list_canvas.configure(scrollregion=bbox)

        def _on_canvas_resize(event):
            list_canvas.itemconfig(_list_win, width=event.width)
            # Also re-sync scrollregion so scrollbar thumb reflects new height
            popup.after(10, _update_scrollregion)

        list_inner.bind("<Configure>", _update_scrollregion)
        list_canvas.bind("<Configure>", _on_canvas_resize)

        def _scroll_popup(event):
            # Only scroll when mouse is over the list canvas area
            try:
                cx = list_canvas.winfo_rootx()
                cy = list_canvas.winfo_rooty()
                cw = list_canvas.winfo_width()
                ch = list_canvas.winfo_height()
                if not (cx <= event.x_root <= cx + cw and cy <= event.y_root <= cy + ch):
                    return
            except Exception:
                pass
            num   = getattr(event, "num", 0)
            delta = getattr(event, "delta", 0)
            if num == 4:
                list_canvas.yview_scroll(-1, "units")
            elif num == 5:
                list_canvas.yview_scroll(1, "units")
            elif delta:
                list_canvas.yview_scroll(int(-delta / 120), "units")
            return "break"

        # bind_all on popup so every child widget (rows, labels, buttons) forwards scroll
        popup.bind_all("<MouseWheel>", _scroll_popup)
        popup.bind_all("<Button-4>",   _scroll_popup)
        popup.bind_all("<Button-5>",   _scroll_popup)

        def _cleanup_scroll_binds():
            try:
                popup.unbind_all("<MouseWheel>")
                popup.unbind_all("<Button-4>")
                popup.unbind_all("<Button-5>")
            except Exception:
                pass
        popup.bind("<Destroy>", lambda e: _cleanup_scroll_binds())

        # ── Working order (mutable; written to JSON on close/load) ────────
        _working_order: list[str] = list(_wl.names())

        # ── Drag state ────────────────────────────────────────────────────
        _drag = {"active": False, "name": None, "start_y": 0,
                 "ghost": None, "orig_idx": 0}

        def _build_rows(filter_q=""):
            """Rebuild the popup list from _working_order, applying optional filter."""
            for w in list_inner.winfo_children():
                w.destroy()

            bar_set = set(_working_order[:3])
            display = (_working_order if not filter_q
                       else [n for n in _working_order
                             if filter_q in n.lower()
                             or any(filter_q in t.lower() for t in _wl.get(n))])

            for i, name in enumerate(display):
                tickers  = _wl.get(name)
                preview  = ",  ".join(tickers[:5])
                if len(tickers) > 5:
                    preview += "…"
                is_active = (name == _wl_active_name[0])
                on_bar    = (name in bar_set)
                bg = CLR_ACCENT if is_active else ("#1E1E3A" if i % 2 == 0 else "#16162E")

                row = tk.Frame(list_inner, bg=bg, cursor="hand2")
                row.pack(fill="x")

                # ── Left side ─────────────────────────────────────────────
                left_frame = tk.Frame(row, bg=bg)
                left_frame.pack(side="left", fill="x", expand=True, pady=5)

                if _reorder_mode[0] == "drag":
                    # Drag handle
                    handle = tk.Label(left_frame, text="≡", font=bold,
                                      bg=bg, fg="#555577", cursor="fleur",
                                      padx=6)
                    handle.pack(side="left")

                    def _make_drag_fns(n=name, r=row):
                        def _drag_start(e):
                            _drag["active"] = True
                            _drag["name"]   = n
                            _drag["start_y"] = e.y_root
                            _drag["orig_idx"] = _working_order.index(n) if n in _working_order else 0
                            r.config(relief="raised")
                        def _drag_motion(e):
                            if not _drag["active"] or _drag["name"] != n:
                                return
                            dy = e.y_root - _drag["start_y"]
                            steps = dy // 36       # ~row height
                            new_idx = max(0, min(len(_working_order) - 1,
                                                 _drag["orig_idx"] + steps))
                            if n in _working_order:
                                cur = _working_order.index(n)
                                if cur != new_idx:
                                    _working_order.remove(n)
                                    _working_order.insert(new_idx, n)
                                    _build_rows(filter_var.get().strip().lower())
                        def _drag_end(e):
                            if _drag["active"] and _drag["name"] == n:
                                _drag["active"] = False
                                _wl.set_order(_working_order)
                                _rebuild_wl_bar()
                                _build_rows(filter_var.get().strip().lower())
                        return _drag_start, _drag_motion, _drag_end

                    ds, dm, de = _make_drag_fns()
                    handle.bind("<ButtonPress-1>",   ds)
                    handle.bind("<B1-Motion>",       dm)
                    handle.bind("<ButtonRelease-1>", de)
                else:
                    # Arrow buttons
                    arrows = tk.Frame(left_frame, bg=bg)
                    arrows.pack(side="left", padx=(4, 0))

                    def _make_move(n=name, delta=-1):
                        def _move():
                            if n not in _working_order:
                                return
                            idx = _working_order.index(n)
                            new = max(0, min(len(_working_order) - 1, idx + delta))
                            if new != idx:
                                _working_order.remove(n)
                                _working_order.insert(new, n)
                                _wl.set_order(_working_order)
                                _rebuild_wl_bar()
                                _build_rows(filter_var.get().strip().lower())
                        return _move

                    tk.Button(arrows, text="▲", font=mono, fg="#AAAACC",
                              bg=bg, activebackground="#2A2A5E",
                              relief="flat", bd=0, cursor="hand2",
                              padx=4, pady=0,
                              command=_make_move(name, -1)).pack()
                    tk.Button(arrows, text="▼", font=mono, fg="#AAAACC",
                              bg=bg, activebackground="#2A2A5E",
                              relief="flat", bd=0, cursor="hand2",
                              padx=4, pady=0,
                              command=_make_move(name, 1)).pack()

                # Bar slot badge (1 / 2 / 3) for top-3 entries
                if on_bar and not filter_q:
                    slot = _working_order.index(name) + 1
                    tk.Label(left_frame, text=f" #{slot} ",
                             bg="#00A4EF", fg="white",
                             font=mono, padx=3, pady=0).pack(side="left", padx=(4, 2))

                marker = "▶  " if is_active else "    "
                name_lbl = tk.Label(left_frame, text=marker + name,
                                    bg=bg, fg="white" if is_active else "#D0EEFF",
                                    font=bold, anchor="w")
                name_lbl.pack(side="left")

                # Right: ticker preview
                tk.Label(row, text=preview,
                         bg=bg, fg="#888899" if not is_active else "#D0EEFF",
                         font=mono, anchor="e").pack(side="right", padx=(0, 12))

                # Click row to load (but not drag handle / arrows)
                def _load_and_close(n=name):
                    _wl.set_order(_working_order)
                    _popup_open[0] = False
                    _wl_load_fn[0](n)
                    popup.destroy()

                for w in (row, name_lbl):
                    w.bind("<Button-1>",   lambda e, fn=_load_and_close: fn())
                    w.bind("<MouseWheel>", _scroll_popup)
                    w.bind("<Button-4>",   _scroll_popup)
                    w.bind("<Button-5>",   _scroll_popup)

                # Hover
                def _enter(e, w=row, b=bg, active=is_active):
                    if not active:
                        w.config(bg="#2A2A5E")
                        for c in w.winfo_children():
                            try: c.config(bg="#2A2A5E")
                            except Exception: pass
                def _leave(e, w=row, b=bg):
                    w.config(bg=b)
                    for c in w.winfo_children():
                        try: c.config(bg=b)
                        except Exception: pass
                row.bind("<Enter>", _enter)
                row.bind("<Leave>", _leave)

        def _toggle_mode():
            _reorder_mode[0] = "drag" if _reorder_mode[0] == "arrows" else "arrows"
            if _reorder_mode[0] == "drag":
                mode_btn.config(text="▲▼  Arrow reorder")
                hint_var.set("Drag ≡ handle to reorder  ·  top 3 always on bar")
            else:
                mode_btn.config(text="↕  Drag to reorder")
                hint_var.set("▲▼ arrows to reorder  ·  top 3 always on bar")
            _build_rows(filter_var.get().strip().lower())

        mode_btn.config(command=_toggle_mode)

        def _on_filter(*_):
            q = filter_var.get().strip().lower()
            _build_rows(q)
            if q:   # only jump to top when actively filtering
                list_canvas.yview_moveto(0)

        filter_var.trace_add("write", _on_filter)
        _build_rows()

        # ── Size and position ─────────────────────────────────────────────
        popup.update_idletasks()
        try:
            import ctypes
            work = ctypes.wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(work), 0)
            work_bottom = work.bottom
        except Exception:
            work_bottom = root.winfo_screenheight() - 48

        popup.geometry("100x100+0+0")
        popup.update_idletasks()
        popup_deco_h = max(popup.winfo_rooty() - popup.winfo_y(), 0)

        rx = root.winfo_rootx()
        rw = root.winfo_width()
        bx = rx + 10
        by = _wl_more_btn.winfo_rooty() + _wl_more_btn.winfo_height() + 2
        popup_w = rw - 20
        popup_h = work_bottom - by - popup_deco_h - 4

        popup.geometry(f"{popup_w}x{popup_h}+{bx}+{by}")
        popup.resizable(True, True)
        popup.minsize(400, 200)

        def _init_scrollregion():
            """First-open only: set scrollregion and go to top."""
            b = list_canvas.bbox("all")
            if b:
                list_canvas.configure(scrollregion=b)
            list_canvas.yview_moveto(0)

        def _sync_scrollregion(*_):
            """Keep scrollregion in sync with content — never moves scroll position."""
            b = list_canvas.bbox("all")
            if b:
                list_canvas.configure(scrollregion=b)

        popup.after(50,  _init_scrollregion)
        popup.after(150, _init_scrollregion)

        # Configure fires on every window move/resize — only sync region, never jump to top
        popup.bind("<Configure>", _sync_scrollregion)
        popup.bind("<Escape>", lambda e: [_popup_open.__setitem__(0, False), popup.destroy()])

        def _on_focus_out():
            if popup.winfo_exists() and not filter_entry.focus_get():
                _popup_open[0] = False
                popup.destroy()
        popup.bind("<FocusOut>", lambda e: root.after(100, _on_focus_out))

    _wl_more_btn.config(command=_wl_open_overflow_menu)

    def _rebuild_wl_bar():
        """
        Rebuild the scrollable button bar from the current order.
        All watchlists appear; first 3 are visually highlighted as bar slots.
        """
        for w in _wl_btn_frame.winfo_children():
            w.destroy()
        _wl_btn_refs.clear()

        all_names = _wl.names()
        bar_names = all_names[:3]

        for name in all_names:
            is_active = (name == _wl_active_name[0])
            on_bar    = (name in bar_names)
            if is_active:
                bg_col = CLR_ACCENT
                fg_col = "white"
            elif on_bar:
                bg_col = "#1A3A5E"
                fg_col = "#FFD580"
            else:
                bg_col = "#2A2A4E"
                fg_col = "#D0EEFF"

            slot_prefix = f"#{bar_names.index(name)+1} " if on_bar and not is_active else ""
            btn = tk.Button(
                _wl_btn_frame,
                text=slot_prefix + name,
                bg=bg_col, fg=fg_col,
                font=bold, relief="flat",
                padx=12, pady=8, cursor="hand2",
                activebackground=CLR_ACCENT, activeforeground="white",
            )
            btn.config(command=lambda n=name: _wl_load_fn[0](n))
            btn.pack(side="left", padx=(6, 0), pady=4)
            _wl_btn_refs[name] = btn

            # Forward mousewheel on buttons to bar canvas
            btn.bind("<MouseWheel>", _wl_bar_scroll)
            btn.bind("<Button-4>",   _wl_bar_scroll)
            btn.bind("<Button-5>",   _wl_bar_scroll)

        root.after(60, _wl_bar_update_scroll)

    _rebuild_wl_bar()

    # ── Selected tickers summary bar ──────────────────────────────────────
    summary_frame = tk.Frame(root, bg="#E8F4FD", pady=6, padx=14)
    summary_frame.pack(fill="x")
    tk.Label(summary_frame, text="Selected: ", bg="#E8F4FD",
             fg=CLR_SUBTEXT, font=bold).pack(side="left")
    summary_var = tk.StringVar(value="—")
    tk.Label(summary_frame, textvariable=summary_var, bg="#E8F4FD",
             fg=CLR_ACCENT, font=sel_font, anchor="w",
             wraplength=520, justify="left").pack(side="left", fill="x", expand=True)

    # ── Filter toggle (All / Selected / Unselected) ───────────────────────
    toggle_frame = tk.Frame(root, bg=CLR_BG, pady=4, padx=14)
    toggle_frame.pack(fill="x")
    filter_var = tk.StringVar(value="all")
    _filter_btns = {}

    def _set_filter(val):
        filter_var.set(val)
        for v, (btn, lbl) in _filter_btns.items():
            active = (v == val)
            btn.config(text="☑" if active else "☐",
                       fg=CLR_ACCENT if active else "#AAAAAA")
        _apply_filter()

    for label, val in [("All", "all"), ("Selected", "selected"), ("Unselected", "unselected")]:
        container = tk.Frame(toggle_frame, bg=CLR_BG)
        container.pack(side="left", padx=(0, 16))
        btn = tk.Button(container, text="☑" if val == "all" else "☐",
                        font=tick_font, fg=CLR_ACCENT if val == "all" else "#AAAAAA",
                        bg=CLR_BG, activebackground=CLR_BG,
                        relief="flat", bd=0, cursor="hand2", padx=0, pady=0,
                        command=lambda v=val: _set_filter(v))
        btn.pack(side="left")
        lbl = tk.Label(container, text=label, bg=CLR_BG, fg=CLR_TEXT, font=bold)
        lbl.pack(side="left")
        lbl.bind("<Button-1>", lambda e, v=val: _set_filter(v))
        _filter_btns[val] = (btn, lbl)

    # ── Search bar ────────────────────────────────────────────────────────
    search_frame = tk.Frame(root, bg=CLR_BG, pady=8, padx=14)
    search_frame.pack(fill="x")
    tk.Label(search_frame, text="🔍  Search / Add:", bg=CLR_BG,
             fg=CLR_TEXT, font=bold).pack(side="left")
    search_var = tk.StringVar()
    search_entry = tk.Entry(search_frame, textvariable=search_var,
                            font=mono, relief="flat", bg="white", fg=CLR_TEXT,
                            insertbackground=CLR_ACCENT,
                            highlightthickness=1,
                            highlightcolor=CLR_ACCENT,
                            highlightbackground="#CCCCCC")
    search_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

    # "Add as new" button for single-ticker mode
    add_new_btn = tk.Button(search_frame, text="", bg="#E06C00", fg="white",
                            font=bold, relief="flat", padx=12, pady=4,
                            cursor="hand2")

    # "Select All Results" button — shown only when search is active (DB rows only)
    def _select_visible_db():
        for sym, _ in available:
            if sym in row_frames and row_frames[sym].winfo_ismapped():
                check_vars[sym].set(True)
        _sync_all_buttons()

    select_results_btn = tk.Button(
        search_frame, text="✔  Select All Results",
        bg="#10B981", fg="white",
        font=bold, relief="flat", padx=12, pady=4,
        cursor="hand2", command=_select_visible_db,
    )

    search_entry.focus_set()

    # ── Scrollable main list ──────────────────────────────────────────────
    list_outer = tk.Frame(root, bg=CLR_BG, padx=14)
    list_outer.pack(fill="both", expand=True)

    canvas = tk.Canvas(list_outer, bg=CLR_BG, highlightthickness=0)
    scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    inner = tk.Frame(canvas, bg=CLR_BG)
    canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_canvas_resize(event):
        canvas.itemconfig(canvas_window, width=event.width)
    canvas.bind("<Configure>", _on_canvas_resize)

    def _on_frame_resize(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
    inner.bind("<Configure>", _on_frame_resize)

    _popup_open = [False]  # flag set True while overflow popup is visible

    def _scroll(event):
        if _popup_open[0]:
            return  # popup is active — it handles its own scroll
        try:
            cx = canvas.winfo_rootx()
            cy = canvas.winfo_rooty()
            cw = canvas.winfo_width()
            ch = canvas.winfo_height()
            if not (cx <= event.x_root <= cx + cw and cy <= event.y_root <= cy + ch):
                return
        except Exception:
            return
        if event.num == 4:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            canvas.yview_scroll(1, "units")
        else:
            canvas.yview_scroll(int(-event.delta / 60), "units")
    canvas.bind_all("<MouseWheel>", _scroll)
    canvas.bind_all("<Button-4>",   _scroll)
    canvas.bind_all("<Button-5>",   _scroll)

    check_vars:   dict[str, tk.BooleanVar] = {}
    row_frames:   dict[str, tk.Frame]      = {}
    tick_buttons: dict[str, tk.Button]     = {}
    _session_added: set[str]               = set()

    # ── DB rows ───────────────────────────────────────────────────────────
    def _make_toggle(v, b):
        def _toggle():
            v.set(not v.get())
            b.config(text="☑" if v.get() else "☐",
                     fg=CLR_ACCENT if v.get() else "#AAAAAA")
        return _toggle

    for i, (sym, name) in enumerate(available):
        var = tk.BooleanVar(value=False)
        check_vars[sym] = var

        bg = CLR_ROW_A if i % 2 == 0 else CLR_ROW_B
        row = tk.Frame(inner, bg=bg, pady=ROW_PADY, padx=ROW_PADX)
        row.pack(fill="x")
        row_frames[sym] = row

        btn = tk.Button(row, text="☐", font=tick_font,
                        fg="#AAAAAA", bg=bg, activebackground=bg,
                        relief="flat", bd=0, cursor="hand2", padx=0, pady=0)
        btn.pack(side="left")
        tick_buttons[sym] = btn
        btn.config(command=_make_toggle(var, btn))

        tk.Label(row, text=f"{sym:<8}", font=bold,
                 bg=bg, fg=CLR_ACCENT, anchor="w").pack(side="left")
        tk.Label(row, text=name, font=mono,
                 bg=bg, fg=CLR_SUBTEXT, anchor="w").pack(side="left", fill="x")

    # ── Summary / count helpers ───────────────────────────────────────────
    def _update_summary(*_):
        selected = [sym for sym, v in check_vars.items() if v.get()]
        summary_var.set(", ".join(selected) if selected else "—")

    def _update_count(*_):
        n = sum(v.get() for v in check_vars.values())
        count_var.set(f"{n} selected")

    for v in check_vars.values():
        v.trace_add("write", _update_summary)
        v.trace_add("write", _update_count)

    # ─────────────────────────────────────────────────────────────────────
    # BULK MODE
    # ─────────────────────────────────────────────────────────────────────

    # Container that sits above the DB rows, hidden when not in bulk mode
    bulk_frame = tk.Frame(inner, bg=CLR_BG)
    # (packed/unpacked dynamically — do NOT pack here)

    _bulk_vars:    dict[str, tk.BooleanVar] = {}   # sym → checked in preview
    _bulk_btns:    dict[str, tk.Button]     = {}
    _bulk_rows:    list[tk.Frame]           = []   # all child frames inside bulk_frame
    _bulk_active   = [False]                        # mutable flag
    _bulk_resolve_id = [None]                       # after() id for debounce

    def _clear_bulk_ui():
        for w in _bulk_rows:
            try:
                w.destroy()
            except Exception:
                pass
        _bulk_rows.clear()
        _bulk_vars.clear()
        _bulk_btns.clear()

    def _hide_bulk():
        _bulk_active[0] = False
        _clear_bulk_ui()
        bulk_frame.pack_forget()

    def _show_bulk_loading(symbols: list[str]):
        """Replace bulk area with a 'Resolving N tickers…' spinner row."""
        _clear_bulk_ui()
        bulk_frame.pack(fill="x", before=list(row_frames.values())[0]
                        if row_frames else None)

        lbl = tk.Label(bulk_frame,
                       text=f"  Resolving {len(symbols)} ticker{'s' if len(symbols) != 1 else ''}…",
                       bg="#FFF9E6", fg="#8B6914", font=bold,
                       anchor="w", pady=6, padx=14)
        lbl.pack(fill="x")
        _bulk_rows.append(lbl)

    def _render_bulk_preview(resolved: list[tuple[str, str, bool]]):
        """
        resolved: list of (symbol, name, in_db)
        Renders the preview list + Add All button inside bulk_frame.
        """
        _clear_bulk_ui()

        if not resolved:
            lbl = tk.Label(bulk_frame,
                           text="  No valid tickers found in input.",
                           bg="#FFF0F0", fg="#8B1414", font=bold,
                           anchor="w", pady=6, padx=14)
            lbl.pack(fill="x")
            _bulk_rows.append(lbl)
            return

        # ── Header row with Add All button ────────────────────────────────
        hdr_row = tk.Frame(bulk_frame, bg="#E8F4FD", pady=5, padx=14)
        hdr_row.pack(fill="x")
        _bulk_rows.append(hdr_row)

        tk.Label(hdr_row,
                 text=f"  {len(resolved)} ticker{'s' if len(resolved) != 1 else ''} ready to add — tick/untick individually or:",
                 bg="#E8F4FD", fg=CLR_SUBTEXT, font=bold).pack(side="left")

        add_all_btn = tk.Button(
            hdr_row,
            text=f"＋  Add all {len(resolved)}",
            bg=CLR_ACCENT, fg="white",
            font=bold, relief="flat", padx=10, pady=4,
            cursor="hand2",
            command=_commit_bulk,
        )
        add_all_btn.pack(side="right", padx=(0, 4))

        # ── Divider ───────────────────────────────────────────────────────
        div = tk.Frame(bulk_frame, bg="#CCCCCC", height=1)
        div.pack(fill="x")
        _bulk_rows.append(div)

        # ── One row per resolved symbol ───────────────────────────────────
        for i, (sym, name, in_db) in enumerate(resolved):
            bg = CLR_ROW_A if i % 2 == 0 else CLR_ROW_B
            row = tk.Frame(bulk_frame, bg=bg, pady=ROW_PADY, padx=ROW_PADX)
            row.pack(fill="x")
            _bulk_rows.append(row)

            var = tk.BooleanVar(value=True)
            _bulk_vars[sym] = var

            sym_colour = CLR_ACCENT if in_db else "#E06C00"

            btn = tk.Button(row, text="☑", font=tick_font,
                            fg=CLR_ACCENT, bg=bg, activebackground=bg,
                            relief="flat", bd=0, cursor="hand2", padx=0, pady=0)
            btn.pack(side="left")
            _bulk_btns[sym] = btn

            def _make_bulk_toggle(v, b):
                def _toggle():
                    v.set(not v.get())
                    b.config(text="☑" if v.get() else "☐",
                             fg=CLR_ACCENT if v.get() else "#AAAAAA")
                return _toggle

            btn.config(command=_make_bulk_toggle(var, btn))

            tk.Label(row, text=f"{sym:<8}", font=bold,
                     bg=bg, fg=sym_colour, anchor="w").pack(side="left")

            name_display = name if name else "(unknown)"
            name_suffix  = "  ✦ in DB" if in_db else "  ↓ will download"
            tk.Label(row, text=name_display, font=mono,
                     bg=bg, fg=CLR_SUBTEXT, anchor="w").pack(side="left")
            tk.Label(row, text=name_suffix, font=mono,
                     bg=bg, fg="#AAAAAA" if in_db else "#E06C00",
                     anchor="w").pack(side="left")

    def _commit_bulk():
        """Tick all checked bulk-preview symbols into the main list, then clear."""
        for sym, var in _bulk_vars.items():
            if var.get():
                _add_ticker_sym(sym, _bulk_name_cache.get(sym, ""))
        _hide_bulk()
        search_var.set("")
        _update_summary()
        _update_count()

    _bulk_name_cache: dict[str, str] = {}   # sym → resolved name

    def _resolve_bulk(symbols: list[str]):
        """
        Worker: resolve names for each symbol via Yahoo, one thread per symbol.
        Collects all results then calls _render_bulk_preview on the main thread.
        """
        db_syms = {sym for sym, _ in available}
        db_name = {sym: name for sym, name in available}

        resolved_lock   = threading.Lock()
        resolved_results: list[tuple[str, str, bool]] = []
        pending         = [len(symbols)]

        def _fetch_one(sym):
            name   = ""
            in_db  = sym in db_syms

            if in_db:
                name = db_name[sym]
            else:
                try:
                    url = (
                        "https://query1.finance.yahoo.com/v1/finance/search"
                        f"?q={urllib.parse.quote(sym)}&quotesCount=5&newsCount=0"
                        f"&enableFuzzyQuery=false&enableEnhancedTrivialQuery=true"
                    )
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=4) as resp:
                        data = json.loads(resp.read())
                    for quote in data.get("quotes", []):
                        if quote.get("symbol", "").upper() == sym:
                            name = quote.get("longname") or quote.get("shortname") or ""
                            break
                except Exception:
                    name = ""

            with resolved_lock:
                _bulk_name_cache[sym] = name
                resolved_results.append((sym, name, in_db))
                pending[0] -= 1
                if pending[0] == 0:
                    # Preserve original input order
                    order = {s: i for i, s in enumerate(symbols)}
                    resolved_results.sort(key=lambda t: order.get(t[0], 999))
                    root.after(0, lambda r=list(resolved_results): _render_bulk_preview(r))

        for sym in symbols:
            threading.Thread(target=_fetch_one, args=(sym,), daemon=True).start()

    def _trigger_bulk_mode(symbols: list[str]):
        """Entry point when comma detected. Show loading, kick off resolve."""
        _bulk_active[0] = True
        add_new_btn.pack_forget()          # hide single-add button
        # Clear any old Yahoo suggestion rows
        for w in _ac_rows:
            try:
                w.destroy()
            except Exception:
                pass
        _ac_rows.clear()

        _show_bulk_loading(symbols)
        _resolve_bulk(symbols)

    _ac_buttons:    dict[str, tk.Button] = {}   # sym → live suggestion button
    _ac_row_frames: dict[str, tk.Frame]  = {}   # sym → suggestion row frame

    # ── Single-ticker add (unchanged logic, factored to shared fn) ────────
    def _make_session_toggle(sym, var, btn):
        def _toggle():
            if var.get():
                var.set(False)
                if sym in row_frames:
                    row_frames[sym].destroy()
                    del row_frames[sym]
                if sym in tick_buttons:
                    del tick_buttons[sym]
                if sym in check_vars:
                    del check_vars[sym]
                _session_added.discard(sym)
                _update_summary()
                _update_count()
                q = search_var.get().strip()
                if q and "," not in q:
                    threading.Thread(target=_yahoo_search, args=(q,), daemon=True).start()
            else:
                var.set(True)
                btn.config(text="☑", fg=CLR_ACCENT)
        return _toggle

    def _add_ticker_sym(sym: str, name: str = ""):
        """Tick an existing DB ticker or add a new session ticker row."""
        if sym in check_vars:
            check_vars[sym].set(True)
            if sym in tick_buttons:
                tick_buttons[sym].config(text="☑", fg=CLR_ACCENT)
            # Sync and retire the suggestion row for this sym
            if sym in _ac_buttons:
                try:
                    _ac_buttons[sym].config(text="☑", fg=CLR_ACCENT,
                                            command=lambda: None)
                except Exception:
                    pass
            if sym in _ac_row_frames:
                try:
                    _ac_row_frames[sym].destroy()
                    _ac_row_frames.pop(sym, None)
                    _ac_buttons.pop(sym, None)
                except Exception:
                    pass
            return

        # New ticker — add a row
        if not name:
            name = _bulk_name_cache.get(sym, "(new — will download)")

        var = tk.BooleanVar(value=True)
        check_vars[sym] = var
        _session_added.add(sym)

        bg = CLR_ROW_A if len(check_vars) % 2 == 0 else CLR_ROW_B
        row = tk.Frame(inner, bg=bg, pady=ROW_PADY, padx=ROW_PADX)
        row.pack(fill="x")
        row_frames[sym] = row

        btn = tk.Button(row, text="☑", font=tick_font,
                        fg=CLR_ACCENT, bg=bg, activebackground=bg,
                        relief="flat", bd=0, cursor="hand2", padx=0, pady=0)
        btn.pack(side="left")
        tick_buttons[sym] = btn
        btn.config(command=_make_session_toggle(sym, var, btn))

        tk.Label(row, text=f"{sym:<8}", font=bold,
                 bg=bg, fg="#E06C00", anchor="w").pack(side="left")
        tk.Label(row, text=name, font=mono,
                 bg=bg, fg=CLR_SUBTEXT, anchor="w").pack(side="left")

        var.trace_add("write", _update_summary)
        var.trace_add("write", _update_count)
        var.trace_add("write", _apply_filter)
        _update_summary()
        _update_count()

        # Sync and retire the suggestion row for this sym (session row is now the source of truth)
        if sym in _ac_buttons:
            try:
                _ac_buttons[sym].config(text="☑", fg=CLR_ACCENT,
                                        command=lambda: None)  # one-shot
            except Exception:
                pass
        if sym in _ac_row_frames:
            try:
                _ac_row_frames[sym].destroy()
                _ac_row_frames.pop(sym, None)
                _ac_buttons.pop(sym, None)
            except Exception:
                pass

    # ── Yahoo autocomplete (single-ticker mode) ───────────────────────────
    _ac_after        = None
    _ac_rows         = []
    _ac_last_results = []

    def _clear_suggestions():
        # Unregister any suggestion rows that were registered as session tickers
        for sym in list(_ac_row_frames.keys()):
            if sym in _session_added:
                # Only remove if not deliberately kept (i.e. var is still False / unticked)
                var = check_vars.get(sym)
                if var is None or not var.get():
                    check_vars.pop(sym, None)
                    row_frames.pop(sym, None)
                    tick_buttons.pop(sym, None)
                    _session_added.discard(sym)
                    _bulk_name_cache.pop(sym, None)
        for w in _ac_rows:
            try:
                w.destroy()
            except Exception:
                pass
        _ac_rows.clear()
        _ac_buttons.clear()
        _ac_row_frames.clear()
        _update_summary()
        _update_count()

    def _yahoo_search(q):
        try:
            url = (
                "https://query1.finance.yahoo.com/v1/finance/search"
                f"?q={urllib.parse.quote(q)}&quotesCount=20&newsCount=0"
                f"&enableFuzzyQuery=false&enableCccBoost=false&enableEnhancedTrivialQuery=true"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read())
            quotes = data.get("quotes", [])
            results = [
                (r.get("symbol", ""),
                 r.get("longname") or r.get("shortname") or "")
                for r in quotes if r.get("symbol")
            ]
        except Exception:
            results = []
        root.after(0, lambda: _show_suggestions(results))

    def _show_suggestions(results):
        nonlocal _ac_last_results
        _ac_last_results = results
        _clear_suggestions()

        db_syms = {sym for sym, _ in available}

        # Only show symbols that are not in the DB and not already session-added
        yahoo_only = [
            (sym, name) for sym, name in results
            if sym not in db_syms and sym not in check_vars
        ]

        if not yahoo_only:
            return

        div = tk.Frame(inner, bg="#CCCCCC", height=1)
        div.pack(fill="x")
        _ac_rows.append(div)
        lbl = tk.Label(inner, text="  Yahoo suggestions (max 7)",
                       bg=CLR_BG, fg=CLR_SUBTEXT, font=bold, anchor="w")
        lbl.pack(fill="x")
        _ac_rows.append(lbl)

        for i, (sym, name) in enumerate(yahoo_only):
            bg = CLR_ROW_A if i % 2 == 0 else CLR_ROW_B
            row = tk.Frame(inner, bg=bg, pady=ROW_PADY, padx=ROW_PADX)
            row.pack(fill="x")
            _ac_rows.append(row)

            # Register this suggestion row as a first-class session ticker so
            # ticking it never needs to spawn a new row elsewhere.
            var = tk.BooleanVar(value=False)
            check_vars[sym]  = var
            row_frames[sym]  = row
            _session_added.add(sym)
            _bulk_name_cache[sym] = name

            var.trace_add("write", _update_summary)
            var.trace_add("write", _update_count)
            var.trace_add("write", _apply_filter)

            btn = tk.Button(row, text="☐", font=tick_font,
                            fg="#AAAAAA", bg=bg, activebackground=bg,
                            relief="flat", bd=0, cursor="hand2", padx=0, pady=0)
            btn.pack(side="left")
            tick_buttons[sym]   = btn
            _ac_buttons[sym]    = btn
            _ac_row_frames[sym] = row

            def _make_suggestion_toggle(s, v, b):
                def _toggle():
                    new_val = not v.get()
                    v.set(new_val)
                    b.config(text="☑" if new_val else "☐",
                             fg=CLR_ACCENT if new_val else "#AAAAAA")
                return _toggle

            btn.config(command=_make_suggestion_toggle(sym, var, btn))
            tk.Label(row, text=f"{sym:<8}", font=bold,
                     bg=bg, fg="#E06C00", anchor="w").pack(side="left")
            tk.Label(row, text=name, font=mono,
                     bg=bg, fg=CLR_SUBTEXT, anchor="w").pack(side="left", fill="x")

    # ── Master search-change handler ──────────────────────────────────────
    def _on_search_change(*_):
        nonlocal _ac_after
        if _ac_after:
            root.after_cancel(_ac_after)
        q = search_var.get().strip()

        # ── Bulk mode: comma detected ─────────────────────────────────────
        if "," in q:
            _clear_suggestions()
            add_new_btn.pack_forget()
            select_results_btn.pack_forget()
            symbols = _parse_bulk(q)
            if symbols:
                if _bulk_resolve_id[0]:
                    root.after_cancel(_bulk_resolve_id[0])
                _bulk_resolve_id[0] = root.after(
                    400, lambda s=symbols: _trigger_bulk_mode(s)
                )
            else:
                _hide_bulk()
            return

        # ── Single mode ───────────────────────────────────────────────────
        if _bulk_active[0]:
            _hide_bulk()

        _clear_suggestions()
        if len(q) < 1:
            add_new_btn.pack_forget()
            select_results_btn.pack_forget()
            return

        _ac_after = root.after(350, lambda: threading.Thread(
            target=_yahoo_search, args=(q,), daemon=True
        ).start())

        exact_match = q.upper() in {sym.upper() for sym, _ in available}
        if not exact_match:
            add_new_btn.config(text=f"＋  Add \"{q.upper()}\" as new ticker")
            add_new_btn.pack(side="left", padx=(8, 0))
        else:
            add_new_btn.pack_forget()

        # Show Select All Results whenever there's an active search
        select_results_btn.pack(side="left", padx=(8, 0))

    search_var.trace_add("write", _on_search_change)

    def _add_new_ticker():
        sym = search_var.get().strip().upper()
        if sym and "," not in sym:
            _add_ticker_sym(sym)

    add_new_btn.config(command=_add_new_ticker)

    # ── Filter (applies to DB rows only; bulk preview sits above) ─────────
    def _apply_filter(*_):
        q    = search_var.get().strip().upper()
        filt = filter_var.get()
        for sym, name in available:
            if sym not in row_frames:
                continue
            text_match = (
                not q or "," in q          # in bulk mode show all DB rows
                or sym.upper().startswith(q)
                or q in sym.upper()
                or q in name.upper()
            )
            if filt == "selected":
                sel_match = check_vars.get(sym, tk.BooleanVar()).get()
            elif filt == "unselected":
                sel_match = not check_vars.get(sym, tk.BooleanVar()).get()
            else:
                sel_match = True
            if text_match and sel_match:
                row_frames[sym].pack(fill="x")
            else:
                row_frames[sym].pack_forget()
        canvas.yview_moveto(0)

    search_var.trace_add("write", _apply_filter)
    for v in check_vars.values():
        v.trace_add("write", _apply_filter)

    # ── Bottom bar ────────────────────────────────────────────────────────
    ctrl = tk.Frame(root, bg=CLR_BG, pady=10, padx=14)
    ctrl.pack(side="bottom", fill="x")

    btn_cfg = dict(font=bold, relief="flat", bd=0, padx=12, pady=7, cursor="hand2")

    def _sync_all_buttons():
        for sym, v in check_vars.items():
            if sym in tick_buttons:
                tick_buttons[sym].config(
                    text="☑" if v.get() else "☐",
                    fg=CLR_ACCENT if v.get() else "#AAAAAA"
                )

    def _select_all():
        for sym in list(row_frames):
            if row_frames[sym].winfo_ismapped():
                check_vars[sym].set(True)
        _sync_all_buttons()

    def _clear_all():
        for v in check_vars.values():
            v.set(False)
        _sync_all_buttons()

    # ── Watchlist load / save / delete ────────────────────────────────────

    def _wl_load(name: str):
        """Tick exactly the symbols in the named watchlist, clear everything else."""
        symbols = set(_wl.get(name))
        if not symbols:
            return
        for v in check_vars.values():
            v.set(False)
        for sym in _wl.get(name):
            if sym not in check_vars:
                _add_ticker_sym(sym, "")
            else:
                check_vars[sym].set(True)
        _sync_all_buttons()
        _set_filter("all")
        search_var.set("")
        # Highlight the active preset button, preserving bar-slot colours for others
        _wl_active_name[0] = name
        bar_names = _wl.names()[:3]
        for n, b in _wl_btn_refs.items():
            if n == name:
                b.config(bg=CLR_ACCENT, fg="white")
            elif n in bar_names:
                b.config(bg="#1A3A5E", fg="#FFD580")
            else:
                b.config(bg="#2A2A4E", fg="#D0EEFF")
        # Scroll the active button into view on the bar canvas
        btn = _wl_btn_refs.get(name)
        if btn:
            def _scroll_to_btn():
                try:
                    bx = btn.winfo_x()
                    bw = btn.winfo_width()
                    total_w = _wl_btn_frame.winfo_reqwidth()
                    if total_w > 0:
                        frac = bx / total_w
                        _wl_bar_canvas.xview_moveto(max(0.0, frac - 0.05))
                except Exception:
                    pass
            root.after(80, _scroll_to_btn)

    def _wl_save_dialog():
        """Small modal: enter a name and save the current selection."""
        selected = [sym for sym, v in check_vars.items() if v.get()]
        if not selected:
            return

        dlg = tk.Toplevel(root)
        dlg.title("Save Watchlist")
        dlg.configure(bg=CLR_BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text="Save current selection as:",
                 bg=CLR_BG, fg=CLR_TEXT, font=bold).pack(anchor="w", padx=20, pady=(16, 4))

        name_var = tk.StringVar()
        # Pre-fill if current selection exactly matches an existing watchlist
        for n in _wl.names():
            if set(_wl.get(n)) == set(selected):
                name_var.set(n)
                break

        entry = tk.Entry(dlg, textvariable=name_var, font=mono,
                         width=28, relief="flat",
                         highlightthickness=1,
                         highlightcolor=CLR_ACCENT,
                         highlightbackground="#CCCCCC")
        entry.pack(padx=20, pady=(4, 4))
        entry.focus_set()
        entry.select_range(0, "end")

        tk.Label(dlg, text=f"  {', '.join(selected)}",
                 bg=CLR_BG, fg=CLR_SUBTEXT, font=mono,
                 wraplength=320, justify="left",
                 padx=20).pack(anchor="w", padx=20, pady=(0, 12))

        # Pin to top checkbox
        pin_frame = tk.Frame(dlg, bg=CLR_BG)
        pin_frame.pack(anchor="w", padx=20, pady=(0, 8))
        pin_var = tk.BooleanVar(value=False)
        pin_btn = tk.Button(pin_frame, text="☐", font=tick_font,
                            fg="#AAAAAA", bg=CLR_BG, activebackground=CLR_BG,
                            relief="flat", bd=0, cursor="hand2", padx=0, pady=0)
        pin_btn.pack(side="left")
        tk.Label(pin_frame, text="  Move to top of list (always on bar)",
                 bg=CLR_BG, fg=CLR_SUBTEXT, font=bold).pack(side="left")

        def _toggle_pin():
            pin_var.set(not pin_var.get())
            pin_btn.config(text="☑" if pin_var.get() else "☐",
                           fg=CLR_ACCENT if pin_var.get() else "#AAAAAA")
        pin_btn.config(command=_toggle_pin)

        btn_row = tk.Frame(dlg, bg=CLR_BG)
        btn_row.pack(fill="x", padx=20, pady=(0, 16))

        def _do_save():
            name = name_var.get().strip()
            if not name:
                return
            _wl.save(name, selected)
            if pin_var.get():
                order = _wl.names()
                if name in order:
                    order.remove(name)
                order.insert(0, name)
                _wl.set_order(order)
            _rebuild_wl_bar()
            dlg.destroy()

        tk.Button(btn_row, text="✓  Save",
                  bg=CLR_ACCENT, fg="white",
                  font=bold, relief="flat", padx=16, pady=6,
                  cursor="hand2", command=_do_save).pack(side="right")
        tk.Button(btn_row, text="Cancel",
                  bg="#E5E7EB", fg=CLR_TEXT,
                  font=bold, relief="flat", padx=12, pady=6,
                  cursor="hand2", command=dlg.destroy).pack(side="right", padx=(0, 8))

        dlg.bind("<Return>", lambda e: _do_save())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        px = root.winfo_x() + (root.winfo_width()  - dlg.winfo_width())  // 2
        py = root.winfo_y() + (root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{px}+{py}")
        dlg.wait_window()

    def _wl_delete_dialog():
        """Small modal: pick a watchlist to delete."""
        names = _wl.names()
        if not names:
            return

        dlg = tk.Toplevel(root)
        dlg.title("Delete Watchlist")
        dlg.configure(bg=CLR_BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text="Delete which watchlist?",
                 bg=CLR_BG, fg=CLR_TEXT, font=bold,
                 padx=20, pady=12).pack(anchor="w", padx=20, pady=(16, 0))

        chosen_var = tk.StringVar(value=names[0])
        for name in names:
            symbols_preview = ", ".join(_wl.get(name)[:6])
            if len(_wl.get(name)) > 6:
                symbols_preview += "…"
            row = tk.Frame(dlg, bg=CLR_BG)
            row.pack(fill="x", padx=20, pady=2)
            tk.Radiobutton(
                row, variable=chosen_var, value=name,
                bg=CLR_BG, activebackground=CLR_BG,
                selectcolor="#D0EEFF",
            ).pack(side="left")
            tk.Label(row, text=f"{name}  ", bg=CLR_BG, fg=CLR_ACCENT,
                     font=bold).pack(side="left")
            tk.Label(row, text=symbols_preview, bg=CLR_BG,
                     fg=CLR_SUBTEXT, font=mono).pack(side="left")

        btn_row = tk.Frame(dlg, bg=CLR_BG)
        btn_row.pack(fill="x", padx=20, pady=(12, 16))

        def _do_delete():
            _wl.delete(chosen_var.get())
            _rebuild_wl_bar()
            dlg.destroy()

        tk.Button(btn_row, text="✖  Delete",
                  bg="#EF4444", fg="white",
                  font=bold, relief="flat", padx=16, pady=6,
                  cursor="hand2", command=_do_delete).pack(side="right")
        tk.Button(btn_row, text="Cancel",
                  bg="#E5E7EB", fg=CLR_TEXT,
                  font=bold, relief="flat", padx=12, pady=6,
                  cursor="hand2", command=dlg.destroy).pack(side="right", padx=(0, 8))

        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        px = root.winfo_x() + (root.winfo_width()  - dlg.winfo_width())  // 2
        py = root.winfo_y() + (root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{px}+{py}")
        dlg.wait_window()

    # Wire the forward refs so the bar buttons can call these
    _wl_load_fn[0]   = _wl_load
    _wl_save_fn[0]   = _wl_save_dialog
    _wl_delete_fn[0] = _wl_delete_dialog
    _rebuild_wl_bar()   # rebuild now that functions are live

    tk.Button(ctrl, text="✔  Select All", bg="#10B981", fg=CLR_BTN_FG,
              activebackground="#0D9E6E",
              command=_select_all, **btn_cfg).pack(side="left", padx=(0, 8))
    tk.Button(ctrl, text="✖  Clear All", bg="#EF4444", fg=CLR_BTN_FG,
              activebackground="#CC3333",
              command=_clear_all, **btn_cfg).pack(side="left")

    count_var = tk.StringVar(value="0 selected")
    tk.Label(ctrl, textvariable=count_var, bg=CLR_BG,
             fg=CLR_SUBTEXT, font=mono).pack(side="left", padx=14)

    years_frame = tk.Frame(ctrl, bg=CLR_BG)
    years_frame.pack(side="left", padx=(0, 14))
    tk.Label(years_frame, text="History:", bg=CLR_BG,
             fg=CLR_SUBTEXT, font=bold).pack(side="left", padx=(0, 6))
    years_var = tk.StringVar(value=str(YEARS_DEFAULT))
    tk.Entry(years_frame, textvariable=years_var, font=mono,
             width=4, relief="flat",
             highlightthickness=1,
             highlightcolor=CLR_ACCENT,
             highlightbackground="#CCCCCC").pack(side="left")
    tk.Label(years_frame, text="yrs", bg=CLR_BG,
             fg=CLR_SUBTEXT, font=mono).pack(side="left", padx=(4, 0))

    refresh_var     = tk.BooleanVar(value=False)
    export_var      = tk.BooleanVar(value=False)
    show_charts_var = tk.BooleanVar(value=True)

    def _go():
        nonlocal result_tickers, result_years, result_refresh, result_export
        result_tickers = [sym for sym, v in check_vars.items() if v.get()]
        try:
            result_years = max(1, int(years_var.get()))
        except ValueError:
            result_years = YEARS_DEFAULT
        result_refresh = refresh_var.get()
        result_export  = export_var.get()

        if not result_tickers:
            return  # nothing selected, stay open

        _saved_geometry = root.geometry()

        hdr.pack_forget()
        wl_frame.pack_forget()
        summary_frame.pack_forget()
        toggle_frame.pack_forget()
        search_frame.pack_forget()
        list_outer.pack_forget()
        ctrl.pack_forget()
        status_panel.pack(fill="both", expand=True)
        root.geometry(_saved_geometry)
        root.update_idletasks()
        if _run_state is not None:
            _run_state["selected"]      = result_tickers
            _run_state["years_back"]    = result_years
            _run_state["force_refresh"] = result_refresh
            _run_state["do_export"]     = result_export
            _run_state["show_charts"]   = show_charts_var.get()
        root.quit()

    def _make_toggle_btn(frame, label, var, side="right", padx=(0, 8)):
        container = tk.Frame(frame, bg=CLR_BG)
        container.pack(side=side, padx=padx)
        btn = tk.Button(container,
                        text="☑" if var.get() else "☐",
                        font=tick_font,
                        fg=CLR_ACCENT if var.get() else "#AAAAAA",
                        bg=CLR_BG, activebackground=CLR_BG,
                        relief="flat", bd=0, cursor="hand2", padx=0, pady=0)
        btn.pack(side="left")
        tk.Label(container, text=label, bg=CLR_BG,
                 fg=CLR_SUBTEXT, font=bold).pack(side="left")

        def _toggle():
            var.set(not var.get())
            btn.config(text="☑" if var.get() else "☐",
                       fg=CLR_ACCENT if var.get() else "#AAAAAA")

        btn.config(command=_toggle)

    tk.Button(ctrl, text="▶  Go", bg=CLR_ACCENT, fg=CLR_BTN_FG,
              activebackground="#0082C8",
              command=_go, **btn_cfg).pack(side="right")
    _make_toggle_btn(ctrl, "Show charts",  show_charts_var, side="right", padx=(0, 8))
    _make_toggle_btn(ctrl, "Re-download",  refresh_var,     side="right", padx=(0, 8))
    _make_toggle_btn(ctrl, "Export files", export_var,      side="right", padx=(0, 8))

    # ── Status panel (hidden until Go is pressed) ─────────────────────────
    status_panel = tk.Frame(root, bg=CLR_BG)
    # Hidden label keeps the tick_font in the widget tree so tkinter's
    # font scaling reference doesn't shift when the picker hides its checkboxes
    tk.Label(status_panel, text="", font=tick_font, bg=CLR_BG).place(x=0, y=0)

    status_hdr = tk.Frame(status_panel, bg=CLR_ACCENT, pady=12)
    status_hdr.pack(fill="x")
    tk.Label(status_hdr, text="Fundamentals Dashboard",
             bg=CLR_ACCENT, fg="white", font=hdr_bold).pack()
    status_title_var = tk.StringVar(value="Loading…")
    tk.Label(status_hdr, textvariable=status_title_var,
             bg=CLR_ACCENT, fg="#D0EEFF", font=hdr_sub).pack()

    log_outer = tk.Frame(status_panel, bg=CLR_BG, padx=20, pady=10)
    log_outer.pack(fill="both", expand=True)
    log_text = tk.Text(
        log_outer,
        font=mono,
        bg="white", fg=CLR_TEXT,
        relief="flat",
        wrap="word",
        highlightthickness=1,
        highlightbackground="#CCCCCC",
        cursor="arrow",
    )
    # Allow selection/copy but block typing
    log_text.bind("<Key>", lambda e: "break" if e.keysym not in ("c", "C") or not (e.state & 0x4) else None)
    log_scroll = ttk.Scrollbar(log_outer, command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side="right", fill="y")
    log_text.pack(fill="both", expand=True)

    status_bottom = tk.Frame(status_panel, bg=CLR_BG, pady=10, padx=20)
    status_bottom.pack(fill="x")
    run_again_btn = tk.Button(
        status_bottom,
        text="↺  Run Again",
        bg=CLR_ACCENT, fg="white",
        font=bold, relief="flat", padx=16, pady=8,
        cursor="hand2",
    )

    def _run_again():
        nonlocal result_tickers, result_years, result_refresh, result_export
        result_tickers = []
        _saved_geo = root.geometry()
        run_again_btn.pack_forget()
        exit_btn.pack_forget()
        status_panel.pack_forget()
        for v in check_vars.values():
            v.set(False)
        _sync_all_buttons()
        _update_summary()
        _update_count()
        search_var.set("")
        refresh_var.set(False)
        export_var.set(False)
        show_charts_var.set(True)
        log_text.delete("1.0", "end")
        status_title_var.set("Loading…")
        hdr.pack(fill="x")
        wl_frame.pack(fill="x")
        summary_frame.pack(fill="x")
        toggle_frame.pack(fill="x")
        search_frame.pack(fill="x")
        list_outer.pack(fill="both", expand=True)
        ctrl.pack(fill="x")
        root.geometry(_saved_geo)
        root.update_idletasks()
        # Re-enter mainloop on the SAME window so user can pick again.
        # _go() will write the new selection to _run_state and call root.quit(),
        # which returns here, and then we return to main()'s _root.mainloop() call
        # which also exits — main() reads _run_state for the new selection.
        # root.mainloop()

    user_exited = False

    def _do_exit():
        nonlocal user_exited
        user_exited = True
        root.destroy()

    exit_btn = tk.Button(
        status_bottom,
        text="✕  Exit",
        bg="#CC3333", fg="white",
        font=bold, relief="flat", padx=16, pady=8,
        cursor="hand2",
        command=_do_exit,
    )

    run_again_btn.config(command=_run_again)
    root.bind("<Return>", lambda e: _go())

    def _on_close():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()
    return result_tickers, result_years, result_refresh, result_export, root, log_text, status_title_var, run_again_btn, status_bottom, exit_btn, user_exited

def post_status(log_text, status_title_var, message, title=None):
    """
    Append a line to the status window log and optionally update the subtitle.
    Safe to call from the main thread. Silently no-ops if the window has been destroyed.
    """
    try:
        if log_text is None or not log_text.winfo_exists():
            return
        if title:
            status_title_var.set(title)
        log_text.insert("end", message + "\n")
        log_text.see("end")
        log_text.update_idletasks()
    except Exception:
        pass


# ── Manual entry fallback (no DB) ─────────────────────────────────────────────

def _manual_entry_fallback() -> tuple[list[str], int, bool, bool]:
    result_tickers: list[str] = []
    result_years:   int       = YEARS_DEFAULT

    root = tk.Tk()
    root.title("Enter Tickers")
    root.configure(bg=CLR_BG)
    root.resizable(True, True)
    root.geometry("440x220")
    root.eval("tk::PlaceWindow . center")

    mono = tkfont.Font(family="Consolas", size=11)
    bold = tkfont.Font(family="Consolas", size=11, weight="bold")

    tk.Label(root,
             text="No saved tickers found.\nEnter symbols separated by commas:",
             bg=CLR_BG, fg=CLR_TEXT, font=bold, pady=14).pack()

    entry_var = tk.StringVar()
    entry = tk.Entry(root, textvariable=entry_var, font=mono,
                     width=36, relief="flat",
                     highlightthickness=1,
                     highlightcolor=CLR_ACCENT,
                     highlightbackground="#CCCCCC")
    entry.pack(pady=4)
    entry.focus_set()

    yf = tk.Frame(root, bg=CLR_BG)
    yf.pack(pady=6)
    tk.Label(yf, text="History:", bg=CLR_BG, fg=CLR_TEXT, font=bold).pack(side="left", padx=(0, 6))
    years_var = tk.StringVar(value=str(YEARS_DEFAULT))
    tk.Entry(yf, textvariable=years_var, font=mono,
             width=4, relief="flat",
             highlightthickness=1,
             highlightcolor=CLR_ACCENT,
             highlightbackground="#CCCCCC").pack(side="left")
    tk.Label(yf, text="yrs", bg=CLR_BG, fg=CLR_TEXT, font=mono).pack(side="left", padx=(4, 0))

    def _go():
        nonlocal result_tickers, result_years
        result_tickers = _parse_bulk(entry_var.get()) or [
            s.strip().upper() for s in entry_var.get().split(",") if s.strip()
        ]
        try:
            result_years = max(1, int(years_var.get()))
        except ValueError:
            result_years = YEARS_DEFAULT
        root.destroy()

    tk.Button(root, text="▶  Go", bg=CLR_ACCENT, fg="white",
              font=bold, relief="flat", padx=16, pady=8,
              command=_go).pack(pady=10)
    root.bind("<Return>", lambda e: _go())
    root.mainloop()
    return result_tickers, result_years, False, False