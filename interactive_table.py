"""
interactive_table.py
────────────────────
Sortable Tkinter table windows for the IT Fundamentals Dashboard.

Public API
----------
show_stock_table_growth(data_list, colors, years_back)  → None
show_stock_table_valuation(data_list, colors, years_back) → None
show_etf_table(etf_list, colors, years_back)             → None

NOTE: the old single show_stock_table() has been split into two windows —
Growth & Quality, and Valuation/Balance Sheet/Capital Return — per the
2026-06-19 scorecard redesign. Both pull from the same data_list, so the
CSV export in the main dashboard script stays combined (single source of
truth for the underlying numbers; the split only affects on-screen display).
"""

import tkinter as tk
from tkinter import ttk, font as tkfont
import numpy as np


# ── Palette — matches ticker_picker / main.py ─────────────────────────────────
CLR_ACCENT   = "#00A4EF"
CLR_BG       = "#F7F9FC"
CLR_HDR_BG   = "#E8F4FD"
CLR_ROW_A    = "#FFFFFF"
CLR_ROW_B    = "#EFF4FA"
CLR_TEXT     = "#1A1A2E"
CLR_SUBTEXT  = "#555577"
CLR_GREEN    = "#D4EDDA"
CLR_YELLOW   = "#FFF3CD"
CLR_RED      = "#F8D7DA"
CLR_NEUTRAL  = "#F0F0F0"
CLR_NAME     = "#EAF4FB"

FG_GREEN  = "#155724"
FG_RED    = "#721C24"
FG_GREY   = "#AAAAAA"


# ── Shared calculation helpers ────────────────────────────────────────────────
# These mirror the helpers of the same name in the main dashboard script.
# Duplicated here (rather than imported) so this module has no dependency
# on the main script — keeps it a standalone, drop-in table widget.

def _latest(values):
    """Return the last non-None value, or np.nan."""
    for v in reversed(values):
        if v is not None:
            return float(v)
    return np.nan


def _avg_last_n(values, n):
    clean = [v for v in values if v is not None]
    subset = clean[-n:]
    return round(sum(subset) / len(subset), 2) if subset else None


def _cagr_pct(values, n_years):
    """
    Generic CAGR helper, shared logic for price/eps/revenue/fcf/bvps CAGR.
    Looks back n_years from the end of the list. Returns None if there
    isn't enough clean history or the start value isn't usable.
    """
    clean = [v for v in values if v is not None]
    if len(clean) < n_years + 1:
        return None
    end, start = clean[-1], clean[-(n_years + 1)]
    if start is None or end is None or start <= 0:
        return None
    return round(((end / start) ** (1 / n_years) - 1) * 100, 2)


def _cagr_full_window(values, years_list):
    """
    Full-window CAGR: from the first valid (year, value) pair to the last,
    regardless of how many years are in the window. This is the "always
    start to end of search window" version, distinct from fixed 3yr/5yr.
    """
    pairs = [(y, v) for y, v in zip(years_list, values) if v is not None and v > 0]
    if len(pairs) < 2:
        return None
    n = pairs[-1][0] - pairs[0][0]
    if n <= 0:
        return None
    return round(((pairs[-1][1] / pairs[0][1]) ** (1 / n) - 1) * 100, 1)


def _eps_cagr(eps_list, years_list):
    """Kept for backward compatibility — same as _cagr_full_window."""
    return _cagr_full_window(eps_list, years_list)


def _cagr(prices, n):
    """Kept for backward compatibility (ETF table uses this name)."""
    clean = [p for p in prices if p is not None]
    if len(clean) < n + 1:
        return None
    end, start = clean[-1], clean[-(n + 1)]
    if start is None or end is None or start <= 0:
        return None
    return round(((end / start) ** (1 / n) - 1) * 100, 2)


def _margin_trend(margin_list):
    """
    Latest margin minus earliest available margin in the window — a simple
    direction-of-travel indicator ("expanding" vs "compressing") rather
    than a full regression slope.
    """
    clean = [v for v in margin_list if v is not None]
    if len(clean) < 2:
        return None
    return round(clean[-1] - clean[0], 2)


def _shorten_etf_name(name):
    """Strip boilerplate from well-known ETF name templates."""
    import re
    m = re.search(r'State Street (.+?) Select Sector', name)
    if m:
        return "SPDR " + m.group(1)
    m = re.search(r'Vanguard (.+?)(?:\s+Index Fund|\s+ETF)', name)
    if m:
        return "VG " + m.group(1)
    m = re.search(r'iShares (.+?)(?:\s+ETF)', name)
    if m:
        return "iSh " + m.group(1)
    return name


# ── Core sortable table window ────────────────────────────────────────────────

class SortableTable:
    """
    Generic sortable table window.

    Parameters
    ----------
    title       : window title string
    columns     : list of column-id strings  (used internally)
    headings    : list of display headings   (same order as columns)
    rows        : list of dicts  {col_id: (display_str, sort_key, bg_colour)}
    row_labels  : list of ticker symbols shown in the first frozen column
    min_col_w   : minimum column width in pixels
    """

    def __init__(self, title, columns, headings, rows, row_labels,
                 min_col_w=90):
        self.columns    = columns
        self.headings   = headings
        self.rows       = rows
        self.row_labels = row_labels
        self.min_col_w  = min_col_w
        self._sort_col  = None
        self._sort_asc  = False

        self.root = tk.Toplevel()
        self.root.title(title)
        self.root.configure(bg=CLR_BG)
        self.root.resizable(True, True)

        mono      = tkfont.Font(family="Consolas", size=12)
        hdr_bold  = tkfont.Font(family="Consolas", size=16, weight="bold")
        hdr_sub   = tkfont.Font(family="Consolas", size=12)

        hdr = tk.Frame(self.root, bg=CLR_ACCENT, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text=title, bg=CLR_ACCENT, fg="white",
                 font=hdr_bold).pack()
        tk.Label(hdr, text="Click any column header to sort  ▲▼",
                 bg=CLR_ACCENT, fg="#D0EEFF", font=hdr_sub).pack()

        frame = tk.Frame(self.root, bg=CLR_BG)
        frame.pack(fill="both", expand=True, padx=14, pady=10)

        vsb = ttk.Scrollbar(frame, orient="vertical")
        hsb = ttk.Scrollbar(frame, orient="horizontal")
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")

        all_cols = tuple(columns)
        self.tv = ttk.Treeview(
            frame,
            columns=all_cols,
            show="tree headings",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
        )
        self.tv.pack(fill="both", expand=True)
        vsb.config(command=self.tv.yview)
        hsb.config(command=self.tv.xview)

        self.tv.heading("#0", text="Ticker",
                        command=lambda: self._sort_by("#0"))
        self.tv.column("#0", width=100, minwidth=80, stretch=False, anchor="center")

        for col, hd in zip(columns, headings):
            self.tv.heading(col, text=hd,
                            command=lambda c=col: self._sort_by(c))
            self.tv.column(col, width=max(min_col_w, len(hd) * 11),
                           minwidth=80, anchor="center")

        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview",
                        background=CLR_ROW_A,
                        fieldbackground=CLR_ROW_A,
                        foreground=CLR_TEXT,
                        font=("Consolas", 15),
                        rowheight=44)
        style.configure("Treeview.Heading",
                        background=CLR_HDR_BG,
                        foreground=CLR_TEXT,
                        font=("Consolas", 15, "bold"),
                        relief="flat")
        style.map("Treeview.Heading",
                  background=[("active", CLR_ACCENT)],
                  foreground=[("active", "white")])
        style.map("Treeview",
                  background=[("selected", CLR_ACCENT)],
                  foreground=[("selected", "white")])

        self.tv.tag_configure("odd",  background=CLR_ROW_A)
        self.tv.tag_configure("even", background=CLR_ROW_B)

        self._populate(self.rows, self.row_labels)

        self._status_var = tk.StringVar(value=f"{len(rows)} rows")
        tk.Label(self.root, textvariable=self._status_var,
                 bg=CLR_BG, fg=CLR_SUBTEXT,
                 font=("Consolas", 13), anchor="w",
                 padx=14).pack(fill="x", pady=(0, 6))

    def _populate(self, rows, row_labels):
        for item in self.tv.get_children():
            self.tv.delete(item)

        for i, (label, row) in enumerate(zip(row_labels, rows)):
            tag = "even" if i % 2 == 0 else "odd"
            values = []
            for col in self.columns:
                cell = row.get(col, ("—", None, CLR_NEUTRAL))
                text, _key, bg = cell
                values.append(text)
            self.tv.insert("", "end", text=label, values=values, tags=(tag,))

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = False

        def sort_key(pair):
            label, row = pair
            if col == "#0":
                return label
            cell = row.get(col, ("—", None, CLR_NEUTRAL))
            _text, key, _bg = cell
            if key is None:
                return (-1e18 if not self._sort_asc else 1e18)
            return key

        pairs = sorted(zip(self.row_labels, self.rows),
                       key=sort_key, reverse=(not self._sort_asc))
        sorted_labels = [p[0] for p in pairs]
        sorted_rows   = [p[1] for p in pairs]

        arrow = " ▲" if self._sort_asc else " ▼"
        for c, hd in zip(self.columns, self.headings):
            indicator = arrow if c == col else ""
            self.tv.heading(c, text=hd + indicator)
        ticker_arrow = arrow if col == "#0" else ""
        self.tv.heading("#0", text="Ticker" + ticker_arrow)

        self._populate(sorted_rows, sorted_labels)
        direction = "low → high" if self._sort_asc else "high → low"
        col_display = self.headings[self.columns.index(col)] if col != "#0" else "Ticker"
        self._status_var.set(
            f"{len(self.rows)} rows  ·  sorted by {col_display}  ({direction})"
        )


def _cell(text, sort_key, bg):
    return (text, sort_key, bg)


# ── Stock scorecard — Table 1: Growth & Quality ───────────────────────────────

def show_stock_table_growth(data_list, colors, years_back):
    """
    Growth & Quality scorecard.
    Columns: Name, Price, Revenue CAGR 3yr/5yr, EPS CAGR (full-window/3yr/5yr),
    FCF/Sh CAGR, Gross/Operating/Net margin (latest + trend), FCF margin.
    """
    columns = [
        "name", "price",
        "rev_cagr_3", "rev_cagr_5",
        "eps_cagr_full", "eps_cagr_3", "eps_cagr_5",
        "fcf_cagr",
        "gross_margin", "gross_trend",
        "op_margin", "op_trend",
        "net_margin", "net_trend",
        "fcf_margin",
    ]
    headings = [
        "Name", "Price",
        "Rev CAGR 3yr", "Rev CAGR 5yr",
        f"EPS CAGR (full {years_back}yr)", "EPS CAGR 3yr", "EPS CAGR 5yr",
        "FCF/Sh CAGR",
        "Gross Margin", "Gross Trend",
        "Op Margin", "Op Trend",
        "Net Margin", "Net Trend",
        "FCF Margin",
    ]

    rows, row_labels = [], []

    for d in data_list:
        row_labels.append(d["symbol"])
        row = {}

        row["name"] = _cell(d.get("name", ""), d.get("name", ""), CLR_NAME)

        cp = d.get("current_price")
        row["price"] = (_cell(f"${float(cp):,.2f}", float(cp), CLR_NAME)
                         if cp is not None else _cell("N/A", None, CLR_NEUTRAL))

        # Revenue CAGR 3yr / 5yr — needs "revps" (already present) scaled
        # by shares is not required since CAGR is scale-invariant; per-share
        # series works fine for a growth-rate calculation.
        for n, key in ((3, "rev_cagr_3"), (5, "rev_cagr_5")):
            val = _cagr_pct(d.get("revps", []), n)
            row[key] = (_cell(f"{val:+.1f}%", val, CLR_GREEN if val >= 0 else CLR_RED)
                        if val is not None else _cell("N/A", None, CLR_NEUTRAL))

        # EPS CAGR — full window (always), plus fixed 3yr/5yr
        val_full = _cagr_full_window(d["eps"], d["years"])
        row["eps_cagr_full"] = (_cell(f"{val_full:+.1f}%", val_full, CLR_GREEN if val_full >= 0 else CLR_RED)
                                if val_full is not None else _cell("N/A", None, CLR_NEUTRAL))
        for n, key in ((3, "eps_cagr_3"), (5, "eps_cagr_5")):
            val = _cagr_pct(d["eps"], n)
            row[key] = (_cell(f"{val:+.1f}%", val, CLR_GREEN if val >= 0 else CLR_RED)
                        if val is not None else _cell("N/A", None, CLR_NEUTRAL))

        # FCF/Sh CAGR — full window
        fcf_cagr = _cagr_full_window(d.get("fcfps", []), d["years"])
        row["fcf_cagr"] = (_cell(f"{fcf_cagr:+.1f}%", fcf_cagr, CLR_GREEN if fcf_cagr >= 0 else CLR_RED)
                           if fcf_cagr is not None else _cell("N/A", None, CLR_NEUTRAL))

        # Margins — expects d["gross_margin"], d["op_margin"], d["net_margin"]
        # as lists of % values per year, populated by the data layer.
        for margin_key, latest_key, trend_key in (
            ("gross_margin", "gross_margin", "gross_trend"),
            ("op_margin",    "op_margin",    "op_trend"),
            ("net_margin",   "net_margin",   "net_trend"),
        ):
            series = d.get(margin_key, [])
            lat = _latest(series)
            if np.isnan(lat):
                row[latest_key] = _cell("N/A", None, CLR_NEUTRAL)
            else:
                bg = CLR_GREEN if lat >= 20 else (CLR_YELLOW if lat >= 10 else CLR_RED)
                row[latest_key] = _cell(f"{lat:.1f}%", lat, bg)

            trend = _margin_trend(series)
            if trend is None:
                row[trend_key] = _cell("N/A", None, CLR_NEUTRAL)
            else:
                bg = CLR_GREEN if trend >= 0 else CLR_RED
                arrow = "▲" if trend >= 0 else "▼"
                row[trend_key] = _cell(f"{arrow} {trend:+.1f}pp", trend, bg)

        # FCF margin (latest) — expects d["fcf_margin"] list of % values
        fcf_margin_series = d.get("fcf_margin", [])
        fm_lat = _latest(fcf_margin_series)
        if np.isnan(fm_lat):
            row["fcf_margin"] = _cell("N/A", None, CLR_NEUTRAL)
        else:
            bg = CLR_GREEN if fm_lat >= 15 else (CLR_YELLOW if fm_lat >= 5 else CLR_RED)
            row["fcf_margin"] = _cell(f"{fm_lat:.1f}%", fm_lat, bg)

        rows.append(row)

    SortableTable(
        title="Stock Scorecard — Growth & Quality",
        columns=columns,
        headings=headings,
        rows=rows,
        row_labels=row_labels,
        min_col_w=120,
    )


# ── Stock scorecard — Table 2: Valuation, Balance Sheet & Capital Return ──────

def show_stock_table_valuation(data_list, colors, years_back):
    """
    Valuation / Balance Sheet / Capital Return scorecard.
    Columns: Name, Price, Fwd/Avg PE, Fwd PE, Trailing PE, 5yr Avg PE,
    Price/FCF, EV/EBITDA, Net Debt/EBITDA, Interest Coverage,
    Buyback Yield, Total Shareholder Yield, ROE latest/3yr avg,
    FCF/Sh latest/3yr avg.
    """
    columns = [
        "name", "price",
        "fwd_avg_pe", "pe_fwd", "pe_trail", "pe_5yr",
        "price_fcf", "ev_ebitda", "net_debt_ebitda", "int_coverage",
        "buyback_yield", "shareholder_yield",
        "roe_lat", "roe_avg",
        "fcf_lat", "fcf_avg",
    ]
    headings = [
        "Name", "Price",
        "Fwd/Avg P/E", "P/E Fwd", "P/E Trail", "P/E 5yr avg",
        "Price/FCF", "EV/EBITDA", "Net Debt/EBITDA", "Interest Coverage",
        "Buyback Yield", "Total Shareholder Yield",
        "ROE % latest", "ROE % 3yr avg",
        "FCF/Sh latest", "FCF/Sh 3yr avg",
    ]

    rows, row_labels = [], []

    for d in data_list:
        row_labels.append(d["symbol"])
        row = {}

        row["name"] = _cell(d.get("name", ""), d.get("name", ""), CLR_NAME)

        cp = d.get("current_price")
        row["price"] = (_cell(f"${float(cp):,.2f}", float(cp), CLR_NAME)
                         if cp is not None else _cell("N/A", None, CLR_NEUTRAL))

        cur_pe = d.get("trailing_pe")
        cur_pe = float(cur_pe) if cur_pe is not None else None
        fwd_pe = d.get("forward_pe")
        fwd_pe = float(fwd_pe) if fwd_pe is not None else None
        avg_pe = _avg_last_n(d["pe"], 5)

        if fwd_pe is not None and avg_pe is not None and avg_pe > 0:
            ratio = round(fwd_pe / avg_pe, 2)
            bg = CLR_GREEN if ratio < 0.8 else (CLR_YELLOW if ratio <= 1.1 else CLR_RED)
            row["fwd_avg_pe"] = _cell(f"{ratio:.2f}x", ratio, bg)
        else:
            row["fwd_avg_pe"] = _cell("N/A", None, CLR_NEUTRAL)

        row["pe_fwd"] = (_cell(f"{fwd_pe:.1f}x", fwd_pe, CLR_YELLOW)
                         if fwd_pe is not None else _cell("N/A", None, CLR_NEUTRAL))
        row["pe_trail"] = (_cell(f"{cur_pe:.1f}x", cur_pe, CLR_YELLOW)
                           if cur_pe is not None else _cell("N/A", None, CLR_NEUTRAL))

        if avg_pe is None:
            row["pe_5yr"] = _cell("N/A", None, CLR_NEUTRAL)
        else:
            bg = CLR_NEUTRAL
            if cur_pe is not None:
                bg = CLR_GREEN if cur_pe < avg_pe else CLR_RED
            row["pe_5yr"] = _cell(f"{avg_pe:.1f}x", avg_pe, bg)

        # Price/FCF — current_price / latest FCF per share
        fcf_lat_raw = _latest(d.get("fcfps", []))
        if cp is not None and not np.isnan(fcf_lat_raw) and fcf_lat_raw > 0:
            pfcf = round(float(cp) / fcf_lat_raw, 1)
            bg = CLR_GREEN if pfcf < 20 else (CLR_YELLOW if pfcf < 30 else CLR_RED)
            row["price_fcf"] = _cell(f"{pfcf:.1f}x", pfcf, bg)
        else:
            row["price_fcf"] = _cell("N/A", None, CLR_NEUTRAL)

        # EV/EBITDA — expects d["ev_ebitda"] precomputed by the data layer
        # (current snapshot, since historical EV/EBITDA needs daily market
        # cap history which yfinance doesn't expose cleanly per fiscal year)
        ev_ebitda = d.get("ev_ebitda")
        if ev_ebitda is not None:
            bg = CLR_GREEN if ev_ebitda < 12 else (CLR_YELLOW if ev_ebitda < 20 else CLR_RED)
            row["ev_ebitda"] = _cell(f"{ev_ebitda:.1f}x", ev_ebitda, bg)
        else:
            row["ev_ebitda"] = _cell("N/A", None, CLR_NEUTRAL)

        # Net Debt/EBITDA — expects d["net_debt_ebitda"] precomputed
        nd_ebitda = d.get("net_debt_ebitda")
        if nd_ebitda is not None:
            bg = CLR_GREEN if nd_ebitda < 1.5 else (CLR_YELLOW if nd_ebitda < 3 else CLR_RED)
            row["net_debt_ebitda"] = _cell(f"{nd_ebitda:.2f}x", nd_ebitda, bg)
        else:
            row["net_debt_ebitda"] = _cell("N/A", None, CLR_NEUTRAL)

        # Interest coverage — expects d["interest_coverage"] precomputed
        int_cov = d.get("interest_coverage")
        if int_cov is not None:
            bg = CLR_GREEN if int_cov > 8 else (CLR_YELLOW if int_cov > 3 else CLR_RED)
            row["int_coverage"] = _cell(f"{int_cov:.1f}x", int_cov, bg)
        else:
            row["int_coverage"] = _cell("N/A", None, CLR_NEUTRAL)

        # Buyback yield — expects d["buyback_yield"] precomputed (%)
        bb_yield = d.get("buyback_yield")
        if bb_yield is not None:
            bg = CLR_GREEN if bb_yield > 0 else CLR_NEUTRAL
            row["buyback_yield"] = _cell(f"{bb_yield:+.1f}%", bb_yield, bg)
        else:
            row["buyback_yield"] = _cell("N/A", None, CLR_NEUTRAL)

        # Total shareholder yield — dividend yield + buyback yield
        div_yield = d.get("dividend_yield")
        if bb_yield is not None and div_yield is not None:
            tsy = round(bb_yield + div_yield, 1)
            bg = CLR_GREEN if tsy > 3 else (CLR_YELLOW if tsy > 0 else CLR_RED)
            row["shareholder_yield"] = _cell(f"{tsy:+.1f}%", tsy, bg)
        else:
            row["shareholder_yield"] = _cell("N/A", None, CLR_NEUTRAL)

        roe_lat = _latest(d["roe"])
        if np.isnan(roe_lat):
            row["roe_lat"] = _cell("N/A", None, CLR_NEUTRAL)
        else:
            bg = CLR_GREEN if roe_lat >= 15 else (CLR_YELLOW if roe_lat >= 8 else CLR_RED)
            row["roe_lat"] = _cell(f"{roe_lat:.1f}%", roe_lat, bg)

        roe_avg = _avg_last_n(d["roe"], 3)
        if roe_avg is None:
            row["roe_avg"] = _cell("N/A", None, CLR_NEUTRAL)
        else:
            bg = CLR_GREEN if roe_avg >= 15 else (CLR_YELLOW if roe_avg >= 8 else CLR_RED)
            row["roe_avg"] = _cell(f"{roe_avg:.1f}%", roe_avg, bg)

        fcf_lat = _latest(d["fcfps"])
        if np.isnan(fcf_lat):
            row["fcf_lat"] = _cell("N/A", None, CLR_NEUTRAL)
        else:
            bg = CLR_GREEN if fcf_lat >= 0 else CLR_RED
            row["fcf_lat"] = _cell(f"${fcf_lat:.2f}", fcf_lat, bg)

        fcf_avg = _avg_last_n(d["fcfps"], 3)
        if fcf_avg is None:
            row["fcf_avg"] = _cell("N/A", None, CLR_NEUTRAL)
        else:
            bg = CLR_GREEN if fcf_avg >= 0 else CLR_RED
            row["fcf_avg"] = _cell(f"${fcf_avg:.2f}", fcf_avg, bg)

        rows.append(row)

    SortableTable(
        title="Stock Scorecard — Valuation, Balance Sheet & Capital Return",
        columns=columns,
        headings=headings,
        rows=rows,
        row_labels=row_labels,
        min_col_w=130,
    )


# ── Backward-compat shim ──────────────────────────────────────────────────────
# In case anything still imports the old combined function name.

def show_stock_table(data_list, colors, years_back):
    show_stock_table_growth(data_list, colors, years_back)
    show_stock_table_valuation(data_list, colors, years_back)


# ── ETF scorecard builder (unchanged from previous version) ──────────────────

def show_etf_table(etf_list, colors, years_back):
    """Build and open the interactive ETF scorecard window."""

    periods = sorted(set([1, 3, 5, 10, years_back - 1]))

    columns  = (
        ["name"]
        + [f"cagr_{p}yr" for p in periods]
        + ["best", "worst", "avg_ret", "vol", "total_ret", "yield_pct"]
    )
    headings = (
        ["Name"]
        + [f"CAGR {p}yr" for p in periods]
        + ["Best Year", "Worst Year", "Avg Return", "Volatility",
           "Total Return", "Yield %"]
    )

    rows       = []
    row_labels = []

    for d in etf_list:
        row_labels.append(d["symbol"])
        row = {}

        _raw_name = d.get("name", "")
        _short_name = _shorten_etf_name(_raw_name)
        row["name"] = _cell(_short_name, _short_name, CLR_NAME)

        for p in periods:
            val = _cagr(d["prices"], p)
            key = f"cagr_{p}yr"
            if val is None:
                row[key] = _cell("N/A", None, CLR_NEUTRAL)
            else:
                bg = CLR_GREEN if val >= 0 else CLR_RED
                row[key] = _cell(f"{val:+.1f}%", val, bg)

        valid_returns = [(yr, r) for yr, r in zip(d["years"], d["annual_returns"])
                        if r is not None]

        if valid_returns:
            best_yr, best_val = max(valid_returns, key=lambda x: x[1])
            row["best"] = _cell(f"{best_yr}  {best_val:+.1f}%", best_val, CLR_GREEN)
        else:
            row["best"] = _cell("N/A", None, CLR_NEUTRAL)

        if valid_returns:
            worst_yr, worst_val = min(valid_returns, key=lambda x: x[1])
            row["worst"] = _cell(f"{worst_yr}  {worst_val:+.1f}%", worst_val, CLR_RED)
        else:
            row["worst"] = _cell("N/A", None, CLR_NEUTRAL)

        if valid_returns:
            avg = round(sum(r for _, r in valid_returns) / len(valid_returns), 1)
            bg  = CLR_GREEN if avg >= 0 else CLR_RED
            row["avg_ret"] = _cell(f"{avg:+.1f}%", avg, bg)
        else:
            row["avg_ret"] = _cell("N/A", None, CLR_NEUTRAL)

        if len(valid_returns) >= 2:
            ret_vals = [r for _, r in valid_returns]
            vol = round(float(np.std(ret_vals, ddof=1)), 1)
            bg  = CLR_GREEN if vol < 12 else (CLR_YELLOW if vol < 20 else CLR_RED)
            row["vol"] = _cell(f"{vol:.1f}%", vol, bg)
        else:
            row["vol"] = _cell("N/A", None, CLR_NEUTRAL)

        clean_prices = [p for p in d["prices"] if p is not None]
        if len(clean_prices) >= 2:
            total_ret = round((clean_prices[-1] / clean_prices[0] - 1) * 100, 1)
            bg = CLR_GREEN if total_ret >= 0 else CLR_RED
            row["total_ret"] = _cell(f"{total_ret:+.0f}%", total_ret, bg)
        else:
            row["total_ret"] = _cell("N/A", None, CLR_NEUTRAL)

        try:
            current_yr = d["years"][-1]
            prior_yr   = current_yr - 1
            annual_dist = sum(
                dist for yr, dist in zip(d["years"], d["distributions"])
                if yr == prior_yr and dist is not None
            )
            if annual_dist == 0:
                annual_dist = sum(
                    dist for yr, dist in zip(d["years"], d["distributions"])
                    if yr == current_yr and dist is not None
                )
            cur_price = d.get("current_price")
            if annual_dist and cur_price and cur_price > 0:
                yp = round(annual_dist / cur_price * 100, 2)
                row["yield_pct"] = _cell(f"{yp:.2f}%", yp, CLR_NAME)
            else:
                row["yield_pct"] = _cell("N/A", None, CLR_NEUTRAL)
        except Exception:
            row["yield_pct"] = _cell("N/A", None, CLR_NEUTRAL)

        rows.append(row)

    SortableTable(
        title="ETF Performance Summary",
        columns=columns,
        headings=headings,
        rows=rows,
        row_labels=row_labels,
        min_col_w=120,
    )