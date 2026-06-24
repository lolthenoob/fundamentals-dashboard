"""
IT Sector Fundamentals Dashboard
=================================
Auto-downloads data from Yahoo Finance via yfinance.
Saves/updates all ticker data to tickers/fundamentals.db (SQLite).

2026-06-19 UPDATE — new metrics layer (growth/quality/valuation/balance
sheet/capital return), split scorecard (see interactive_table.py), chart
panel headline numbers + footer summary line, and per-panel data tables
under each mini-chart in plot_single_ticker. Rule of 40 intentionally
NOT included (mixed semis/software ticker list makes it misleading).

CONFIGURE YOUR TICKERS HERE:
"""

'TICKERS = ["MSFT", "AAPL", "NVDA", "AVGO", "ORCL", "AMD", "QCOM", "TXN", "ACN", "IBM"]'
'TICKERS = ["MSFT"]'

# How many years of annual history to show
'YEARS_BACK = 11'

# ─────────────────────────────────────────────────────────────────────────────
from ticker_picker import pick_tickers, post_status
from interactive_table import (
    show_stock_table_growth, show_stock_table_valuation, show_etf_table,
    show_stock_table_quarterly_earnings, show_stock_table_quarterly_valuation,
)
import app_settings
import warnings
warnings.filterwarnings("ignore")

import configparser
import csv
import dateutil.parser

import sys
import os
import sqlite3
import json
import numpy as np
import matplotlib
matplotlib.use("TkAgg")          # change to "Qt5Agg" if TkAgg isn't available
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
import yfinance as yf
from datetime import datetime, timedelta


# ── Colour palette (cycles if more tickers than colours) ─────────────────────
PALETTE = [
    "#00A4EF","#555555","#76B900","#CC0000","#F80000",
    "#ED1C24","#3253DC","#E4002B","#A100FF","#1F70C1",
    "#F59E0B","#10B981","#8B5CF6","#EC4899","#06B6D4",
]

def get_color(i):
    return PALETTE[i % len(PALETTE)]


# ─────────────────────────────────────────────────────────────────────────────
# 1. DATABASE  (tickers/fundamentals.db)
# ─────────────────────────────────────────────────────────────────────────────

_BASE = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
DB_DIR    = os.path.join(_BASE, "tickers")
DB_OUTPUT = os.path.join(_BASE, "output")
DB_PATH = os.path.join(DB_DIR, "fundamentals.db")

# ─────────────────────────────────────────────────────────────────────────────
# 0b. CHART PREFERENCES  (tickers/chart_prefs.ini)
# ─────────────────────────────────────────────────────────────────────────────

PREFS_PATH = os.path.join(DB_DIR, "chart_prefs.ini")

# Valid chart style choices per series
PANEL_STYLES = {
    "panel1_price":       ("line",),
    "panel2_pe":          ("bar", "line"),
    "panel2_peg":         ("bar", "line"),
    "panel3_eps":         ("bar", "line"),
    "panel3_roe":         ("bar", "line"),
    "panel4_bvps":        ("bar", "line"),
    "panel4_debt":        ("bar", "line"),
    "panel5_ocf":         ("bar", "line"),
    "panel5_fcf":         ("bar", "line"),
    "panel6_rev":         ("bar", "line"),
    "panel6_div":         ("bar", "line"),
}

PANEL_DEFAULTS = {
    "panel1_price":   "line",
    "panel2_pe":      "bar",
    "panel2_peg":     "line",
    "panel3_eps":     "bar",
    "panel3_roe":     "line",
    "panel4_bvps":    "bar",
    "panel4_debt":    "line",
    "panel5_ocf":     "bar",
    "panel5_fcf":     "line",
    "panel6_rev":     "bar",
    "panel6_div":     "line",
}

def load_chart_prefs():
    """Load chart style preferences from INI file, falling back to defaults."""
    cfg = configparser.ConfigParser()
    prefs = dict(PANEL_DEFAULTS)
    if os.path.exists(PREFS_PATH):
        cfg.read(PREFS_PATH)
        if "chart_styles" in cfg:
            for key, valid in PANEL_STYLES.items():
                val = cfg["chart_styles"].get(key, prefs[key]).strip().lower()
                if val in valid:
                    prefs[key] = val
    return prefs

def save_chart_prefs(prefs):
    """Write chart style preferences back to the INI file."""
    os.makedirs(DB_DIR, exist_ok=True)
    cfg = configparser.ConfigParser()
    cfg["chart_styles"] = prefs
    with open(PREFS_PATH, "w") as f:
        cfg.write(f)


def get_db():
    """Return a connection to the SQLite database, creating it if needed."""
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(DB_OUTPUT, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    return conn

def _create_tables(conn):
    conn.executescript("""
        -- One row per ticker (live/analyst data)
        CREATE TABLE IF NOT EXISTS tickers (
            symbol          TEXT PRIMARY KEY,
            name            TEXT,
            current_price   REAL,
            analyst_tp      REAL,
            analyst_low     REAL,
            analyst_high    REAL,
            consensus       TEXT,
            trailing_pe     REAL,
            forward_pe      REAL,
            peg_ratio       REAL,
            last_updated    TEXT
        );

        -- One row per ticker × fiscal year (all per-share fundamentals)
        CREATE TABLE IF NOT EXISTS annual_data (
            symbol          TEXT    NOT NULL,
            fiscal_year     INTEGER NOT NULL,
            price           REAL,
            eps             REAL,
            pe              REAL,
            roe             REAL,
            bvps            REAL,
            debt_assets     REAL,
            ocfps           REAL,
            fcfps           REAL,
            revps           REAL,
            divps           REAL,
            PRIMARY KEY (symbol, fiscal_year),
            FOREIGN KEY (symbol) REFERENCES tickers(symbol)
        );
        CREATE TABLE IF NOT EXISTS etf_data (
            symbol          TEXT    NOT NULL,
            fiscal_year     INTEGER NOT NULL,
            price           REAL,
            distribution    REAL,
            annual_return   REAL,
            PRIMARY KEY (symbol, fiscal_year),
            FOREIGN KEY (symbol) REFERENCES tickers(symbol)
        );

        -- 2026-06-20: one row per ticker × reported quarter (date-keyed, not
        -- fiscal-quarter-int-keyed — see notes in upsert_quarterly). This
        -- table is append-only by design: every run upserts whatever
        -- quarters yfinance currently exposes (~4-5 live), but old quarters
        -- already saved here are NEVER dropped just because yfinance's live
        -- window rolled past them. That's the whole point — it's what lets
        -- a one-off shock quarter (e.g. a macro-driven P/E trough) stay
        -- queryable years later even though yfinance itself only remembers
        -- the last year or so at any given time.
        CREATE TABLE IF NOT EXISTS quarterly_data (
            symbol          TEXT    NOT NULL,
            quarter_end     TEXT    NOT NULL,   -- ISO date 'YYYY-MM-DD', period end
            fiscal_year     INTEGER,            -- derived, display label only
            fiscal_quarter  INTEGER,            -- 1-4, derived, display label only
            earnings_date   TEXT,                -- ISO date of the actual report
            price           REAL,                -- close on/near quarter_end
            eps_actual      REAL,
            eps_estimate    REAL,
            pe              REAL,
            revenue         REAL,
            net_income      REAL,
            gross_margin    REAL,
            op_margin       REAL,
            net_margin      REAL,
            debt            REAL,
            ocf             REAL,
            capex           REAL,
            fcf             REAL,
            cash            REAL,
            price_react_pct REAL,                -- stock move N days post-earnings
            last_updated    TEXT,
            PRIMARY KEY (symbol, quarter_end),
            FOREIGN KEY (symbol) REFERENCES tickers(symbol)
        );
    """)

    conn.commit()

    # Migrate existing DBs that predate trailing_pe / forward_pe / peg_ratio
    for col in ("trailing_pe", "forward_pe", "peg_ratio"):
        try:
            conn.execute(f"ALTER TABLE tickers ADD COLUMN {col} REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migrate existing DBs to add years_stored / history_exhausted
    for col_def in ("years_stored INTEGER", "history_exhausted INTEGER DEFAULT 0"):
        col_name = col_def.split()[0]
        try:
            conn.execute(f"ALTER TABLE tickers ADD COLUMN {col_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    # ── 2026-06-19: new ticker-level snapshot columns ──────────────────────
    # These are current-snapshot values (not per-year history), used by the
    # Valuation/Balance Sheet/Capital Return scorecard.
    for col_def in (
        "ev_ebitda REAL",
        "net_debt_ebitda REAL",
        "interest_coverage REAL",
        "buyback_yield REAL",
        "dividend_yield REAL",
    ):
        try:
            conn.execute(f"ALTER TABLE tickers ADD COLUMN {col_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # ── 2026-06-19: new annual_data columns for margins + raw totals ───────
    # gross/op/net margin stored as % per year; raw revenue/shares stored
    # for buyback-yield calc across years.
    for col_def in (
        "gross_margin REAL",
        "op_margin REAL",
        "net_margin REAL",
        "fcf_margin REAL",
        "shares_out REAL",
    ):
        try:
            conn.execute(f"ALTER TABLE annual_data ADD COLUMN {col_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

def upsert_ticker(conn, d, years_requested):
    now = datetime.now().isoformat(timespec="seconds")
    years_stored      = len(d["years"])
    history_exhausted = 1 if years_stored < years_requested else 0

    conn.execute("""
        INSERT INTO tickers
            (symbol, name, current_price, analyst_tp, analyst_low, analyst_high,
             consensus, trailing_pe, forward_pe, peg_ratio, last_updated,
             years_stored, history_exhausted,
             ev_ebitda, net_debt_ebitda, interest_coverage,
             buyback_yield, dividend_yield)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
            name              = excluded.name,
            current_price     = excluded.current_price,
            analyst_tp        = excluded.analyst_tp,
            analyst_low       = excluded.analyst_low,
            analyst_high      = excluded.analyst_high,
            consensus         = excluded.consensus,
            trailing_pe       = excluded.trailing_pe,
            forward_pe        = excluded.forward_pe,
            peg_ratio         = excluded.peg_ratio,
            last_updated      = excluded.last_updated,
            years_stored      = excluded.years_stored,
            history_exhausted = excluded.history_exhausted,
            ev_ebitda          = excluded.ev_ebitda,
            net_debt_ebitda     = excluded.net_debt_ebitda,
            interest_coverage   = excluded.interest_coverage,
            buyback_yield       = excluded.buyback_yield,
            dividend_yield      = excluded.dividend_yield
    """, (
        d["symbol"], d["name"], d["current_price"],
        d["analyst_tp"], d["analyst_low"], d["analyst_high"],
        d["consensus"], d["trailing_pe"], d["forward_pe"],
        d.get("peg_ratio"), now,
        years_stored, history_exhausted,
        d.get("ev_ebitda"), d.get("net_debt_ebitda"), d.get("interest_coverage"),
        d.get("buyback_yield"), d.get("dividend_yield"),
    ))

    rows = zip(
        d["years"],  d["prices"], d["eps"],   d["pe"],
        d["roe"],    d["bvps"],   d["debt_assets"],
        d["ocfps"],  d["fcfps"],  d["revps"],  d["divps"],
        d.get("gross_margin", [None]*len(d["years"])),
        d.get("op_margin",    [None]*len(d["years"])),
        d.get("net_margin",   [None]*len(d["years"])),
        d.get("fcf_margin",   [None]*len(d["years"])),
        d.get("shares_out",   [None]*len(d["years"])),
    )
    conn.executemany("""
        INSERT INTO annual_data
            (symbol, fiscal_year, price, eps, pe, roe, bvps,
             debt_assets, ocfps, fcfps, revps, divps,
             gross_margin, op_margin, net_margin, fcf_margin, shares_out)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol, fiscal_year) DO UPDATE SET
            price       = excluded.price,
            eps         = excluded.eps,
            pe          = excluded.pe,
            roe         = excluded.roe,
            bvps        = excluded.bvps,
            debt_assets = excluded.debt_assets,
            ocfps       = excluded.ocfps,
            fcfps       = excluded.fcfps,
            revps       = excluded.revps,
            divps       = excluded.divps,
            gross_margin = excluded.gross_margin,
            op_margin    = excluded.op_margin,
            net_margin   = excluded.net_margin,
            fcf_margin   = excluded.fcf_margin,
            shares_out   = excluded.shares_out
    """, [(d["symbol"], yr, p, e, pe, roe, bvps, da, ocf, fcf, rev, div,
           gm, om, nm, fm, so)
          for yr, p, e, pe, roe, bvps, da, ocf, fcf, rev, div, gm, om, nm, fm, so
          in rows])

    conn.commit()
    print(f"    → Saved {d['symbol']} to DB ({len(d['years'])} years)")

def load_ticker_from_db(conn, symbol):
    row = conn.execute(
        "SELECT * FROM tickers WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row is None:
        return None

    rows = conn.execute(
        "SELECT * FROM annual_data WHERE symbol = ? ORDER BY fiscal_year",
        (symbol,)
    ).fetchall()
    if not rows:
        return None

    def col(field):
        return [r[field] for r in rows]

    return {
        "symbol":        row["symbol"],
        "name":          row["name"],
        "years":         col("fiscal_year"),
        "prices":        col("price"),
        "eps":           col("eps"),
        "pe":            col("pe"),
        "roe":           col("roe"),
        "bvps":          col("bvps"),
        "debt_assets":   col("debt_assets"),
        "ocfps":         col("ocfps"),
        "fcfps":         col("fcfps"),
        "revps":         col("revps"),
        "divps":         col("divps"),
        "gross_margin":  col("gross_margin"),
        "op_margin":     col("op_margin"),
        "net_margin":    col("net_margin"),
        "fcf_margin":    col("fcf_margin"),
        "shares_out":    col("shares_out"),
        "current_price": row["current_price"],
        "analyst_tp":    row["analyst_tp"],
        "analyst_low":   row["analyst_low"],
        "analyst_high":  row["analyst_high"],
        "consensus":     row["consensus"],
        "trailing_pe":   row["trailing_pe"],
        "forward_pe":    row["forward_pe"],
        "peg_ratio":     row["peg_ratio"],
        "ev_ebitda":         row["ev_ebitda"],
        "net_debt_ebitda":   row["net_debt_ebitda"],
        "interest_coverage": row["interest_coverage"],
        "buyback_yield":     row["buyback_yield"],
        "dividend_yield":    row["dividend_yield"],
    }

def upsert_quarterly(conn, d):
    """
    Append-only by design — see the quarterly_data table comment in
    _create_tables(). Every call inserts/refreshes exactly the quarters
    present in `d` (whatever yfinance's live ~4-5 quarter window currently
    returns); ON CONFLICT only updates rows matching those dates, so older
    quarters already saved from a previous run that have since rolled out
    of yfinance's window are left completely untouched. Nothing is ever
    deleted here.
    """
    now = datetime.now().isoformat(timespec="seconds")
    n   = len(d["quarter_ends"])

    def get(key):
        return d.get(key, [None] * n)

    rows = zip(
        d["quarter_ends"], get("fiscal_years"), get("fiscal_quarters"),
        get("earnings_dates"), get("prices"), get("eps_actual"),
        get("eps_estimate"), get("pe"), get("revenue"), get("net_income"),
        get("gross_margin"), get("op_margin"), get("net_margin"),
        get("debt"), get("ocf"), get("capex"), get("fcf"), get("cash"),
        get("price_react_pct"),
    )
    # pe needs 4 trailing quarters in *this* fetch to compute (see
    # download_ticker_quarterly) — a quarter can still be in the live
    # window but sitting too early in it to have that depth. COALESCE
    # below keeps whatever real P/E was saved earlier rather than
    # blanking it out just because this refresh's window didn't reach
    # back far enough for it.
    conn.executemany("""
        INSERT INTO quarterly_data
            (symbol, quarter_end, fiscal_year, fiscal_quarter, earnings_date,
             price, eps_actual, eps_estimate, pe, revenue, net_income,
             gross_margin, op_margin, net_margin, debt, ocf, capex, fcf, cash,
             price_react_pct, last_updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol, quarter_end) DO UPDATE SET
            fiscal_year     = excluded.fiscal_year,
            fiscal_quarter  = excluded.fiscal_quarter,
            earnings_date   = excluded.earnings_date,
            price           = excluded.price,
            eps_actual      = excluded.eps_actual,
            eps_estimate    = excluded.eps_estimate,
            pe              = COALESCE(excluded.pe, pe),
            revenue         = excluded.revenue,
            net_income      = excluded.net_income,
            gross_margin    = excluded.gross_margin,
            op_margin       = excluded.op_margin,
            net_margin      = excluded.net_margin,
            debt            = excluded.debt,
            ocf             = excluded.ocf,
            capex           = excluded.capex,
            fcf             = excluded.fcf,
            cash            = excluded.cash,
            price_react_pct = excluded.price_react_pct,
            last_updated    = excluded.last_updated
    """, [
        (d["symbol"], qe, fy, fq, ed, p, epsa, epse, pe, rev, ni,
         gm, om, nm, debt, ocf, capex, fcf, cash, reac, now)
        for qe, fy, fq, ed, p, epsa, epse, pe, rev, ni, gm, om, nm, debt, ocf, capex, fcf, cash, reac
        in rows
    ])

    conn.commit()
    print(f"    \u2192 Saved {d['symbol']} quarterly data to DB ({n} quarter(s))")


def load_quarterly_from_db(conn, symbol, start=None, end=None):
    """
    Read back accumulated quarterly history for a symbol.

    `start`/`end` are optional ISO date strings ('YYYY-MM-DD') bounding
    quarter_end — this is the explicit-date-range display mode (pulling up
    a specific past quarter, e.g. a macro-shock trough, regardless of how
    recent it is). For the simpler "last N quarters" display mode, leave
    start/end blank and slice the result with trim_to_quarters() instead.
    """
    query  = "SELECT * FROM quarterly_data WHERE symbol = ?"
    params = [symbol]
    if start:
        query += " AND quarter_end >= ?"
        params.append(start)
    if end:
        query += " AND quarter_end <= ?"
        params.append(end)
    query += " ORDER BY quarter_end"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return None

    def col(field):
        return [r[field] for r in rows]

    return {
        "symbol":          symbol,
        "quarter_ends":    col("quarter_end"),
        "fiscal_years":    col("fiscal_year"),
        "fiscal_quarters": col("fiscal_quarter"),
        "earnings_dates":  col("earnings_date"),
        "prices":          col("price"),
        "eps_actual":      col("eps_actual"),
        "eps_estimate":    col("eps_estimate"),
        "pe":              col("pe"),
        "revenue":         col("revenue"),
        "net_income":      col("net_income"),
        "gross_margin":    col("gross_margin"),
        "op_margin":       col("op_margin"),
        "net_margin":      col("net_margin"),
        "debt":            col("debt"),
        "ocf":             col("ocf"),
        "capex":           col("capex"),
        "fcf":             col("fcf"),
        "cash":            col("cash"),
        "price_react_pct": col("price_react_pct"),
    }


def upsert_etf(conn, d, years_requested):
    now = datetime.now().isoformat(timespec="seconds")
    years_stored      = len(d["years"])
    history_exhausted = 1 if years_stored < years_requested else 0

    conn.execute("""
        INSERT INTO tickers (symbol, name, current_price, analyst_tp,
            analyst_low, analyst_high, consensus, last_updated,
            years_stored, history_exhausted)
        VALUES (?,?,?,NULL,NULL,NULL,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
            name              = excluded.name,
            current_price     = excluded.current_price,
            consensus         = excluded.consensus,
            last_updated      = excluded.last_updated,
            years_stored      = excluded.years_stored,
            history_exhausted = excluded.history_exhausted
    """, (d["symbol"], d["name"], d["current_price"], "ETF", now,
          years_stored, history_exhausted))

    conn.executemany("""
        INSERT INTO etf_data (symbol, fiscal_year, price, distribution, annual_return)
        VALUES (?,?,?,?,?)
        ON CONFLICT(symbol, fiscal_year) DO UPDATE SET
            price         = excluded.price,
            distribution  = excluded.distribution,
            annual_return = excluded.annual_return
    """, [(d["symbol"], yr, p, dist, ret)
          for yr, p, dist, ret in zip(
              d["years"], d["prices"], d["distributions"], d["annual_returns"])])
    conn.commit()
    print(f"    → Saved {d['symbol']} (ETF) to DB ({len(d['years'])} years)")


def load_etf_from_db(conn, symbol):
    row = conn.execute(
        "SELECT * FROM tickers WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row is None or row["consensus"] != "ETF":
        return None

    rows = conn.execute(
        "SELECT * FROM etf_data WHERE symbol = ? ORDER BY fiscal_year",
        (symbol,)
    ).fetchall()
    if not rows:
        return None

    return {
        "symbol":        row["symbol"],
        "name":          row["name"],
        "quote_type":    "ETF",
        "years":         [r["fiscal_year"]   for r in rows],
        "prices":        [r["price"]         for r in rows],
        "distributions": [r["distribution"]  for r in rows],
        "annual_returns":[r["annual_return"] for r in rows],
        "current_price": row["current_price"],
        "expense_ratio": None,
        "aum":           None,
        "category":      "",
    }

def print_db_summary(conn):
    tickers = conn.execute(
        "SELECT symbol, name, last_updated FROM tickers ORDER BY symbol"
    ).fetchall()
    if not tickers:
        print("  (database is empty)")
        return
    print(f"  {'Symbol':<8} {'Last Updated':<22} Name")
    print(f"  {'-'*8} {'-'*22} {'-'*30}")
    for t in tickers:
        print(f"  {t['symbol']:<8} {t['last_updated']:<22} {t['name']}")


# ─────────────────────────────────────────────────────────────────────────────
# 1b. DEBUG EXPORTS
# ─────────────────────────────────────────────────────────────────────────────

def export_summary_txt(conn):
    path = os.path.join(DB_OUTPUT, "db_summary.txt")
    tickers = conn.execute(
        "SELECT symbol, name, last_updated FROM tickers ORDER BY symbol"
    ).fetchall()
    annual_count = conn.execute("SELECT COUNT(*) FROM annual_data").fetchone()[0]

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"fundamentals.db  —  exported {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 62 + "\n\n")
        f.write(f"  {'Symbol':<8} {'Last Updated':<22} Name\n")
        f.write(f"  {'-'*8} {'-'*22} {'-'*30}\n")
        for t in tickers:
            f.write(f"  {t['symbol']:<8} {t['last_updated']:<22} {t['name']}\n")
        f.write(f"\n  {len(tickers)} tickers  ·  {annual_count} annual rows\n")

    print(f"  → db_summary.txt  ({len(tickers)} tickers)")


def export_full_csv(conn):
    path = os.path.join(DB_OUTPUT, "db_full.csv")
    rows = conn.execute("""
        SELECT
            t.symbol, t.name, a.fiscal_year,
            t.current_price, t.analyst_tp, t.analyst_low, t.analyst_high,
            t.consensus, t.last_updated,
            a.price, a.eps, a.pe, a.roe, a.bvps, a.debt_assets,
            a.ocfps, a.fcfps, a.revps, a.divps,
            a.gross_margin, a.op_margin, a.net_margin, a.fcf_margin, a.shares_out
        FROM annual_data a
        JOIN tickers t ON t.symbol = a.symbol
        ORDER BY t.symbol, a.fiscal_year
    """).fetchall()

    fieldnames = [
        "symbol", "name", "fiscal_year",
        "current_price", "analyst_tp", "analyst_low", "analyst_high",
        "consensus", "last_updated",
        "price", "eps", "pe", "roe", "bvps", "debt_assets",
        "ocfps", "fcfps", "revps", "divps",
        "gross_margin", "op_margin", "net_margin", "fcf_margin", "shares_out",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(zip(fieldnames, row)))

    print(f"  → db_full.csv     ({len(rows)} rows)")


def export_db_health(conn):
    path = os.path.join(DB_OUTPUT, "db_health.txt")
    now  = datetime.now()

    tickers = conn.execute("SELECT * FROM tickers ORDER BY symbol").fetchall()
    issues  = []
    ok      = []

    TICKER_FIELDS = ["name", "current_price", "analyst_tp", "analyst_low",
                     "analyst_high", "consensus", "trailing_pe", "forward_pe"]

    for t in tickers:
        sym      = t["symbol"]
        is_etf   = (t["consensus"] == "ETF")
        sym_issues = []

        for field in TICKER_FIELDS:
            if t[field] is None:
                if field in ("trailing_pe", "forward_pe") and is_etf:
                    continue
                if field in ("analyst_tp", "analyst_low", "analyst_high", "consensus"):
                    sym_issues.append(f"  WARN  {field} is NULL")
                else:
                    sym_issues.append(f"  MISS  {field} is NULL")

        if t["last_updated"]:
            try:
                updated = dateutil.parser.parse(t["last_updated"]).replace(tzinfo=None)
                age_days = (now - updated).days
                if age_days > 180:
                    sym_issues.append(f"  STALE last_updated {age_days}d ago ({t['last_updated'][:10]})")
            except Exception:
                sym_issues.append(f"  WARN  could not parse last_updated: {t['last_updated']}")
        else:
            sym_issues.append(f"  MISS  last_updated is NULL")

        if is_etf:
            rows = conn.execute(
                "SELECT * FROM etf_data WHERE symbol=? ORDER BY fiscal_year", (sym,)
            ).fetchall()
            annual_fields = ["price", "distribution", "annual_return"]
        else:
            rows = conn.execute(
                "SELECT * FROM annual_data WHERE symbol=? ORDER BY fiscal_year", (sym,)
            ).fetchall()
            annual_fields = ["price", "eps", "pe", "roe", "bvps",
                             "debt_assets", "ocfps", "fcfps", "revps"]

        if not rows:
            sym_issues.append(f"  MISS  no annual data rows at all")
        else:
            if len(rows) < 5:
                sym_issues.append(f"  WARN  only {len(rows)} year(s) of history")
            for row in rows:
                yr      = row["fiscal_year"]
                missing = [f for f in annual_fields if row[f] is None]
                if len(missing) == len(annual_fields):
                    sym_issues.append(f"  MISS  {yr}: all fields NULL")
                elif missing:
                    sym_issues.append(f"  WARN  {yr}: NULL in {', '.join(missing)}")

        if sym_issues:
            issues.append((sym, sym_issues))
        else:
            ok.append(sym)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"DB Health Report  —  {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 62 + "\n\n")

        if not issues:
            f.write("  ✓ All tickers look healthy.\n\n")
        else:
            f.write(f"  {len(issues)} ticker(s) with issues:\n\n")
            for sym, sym_issues in issues:
                f.write(f"  [{sym}]\n")
                for line in sym_issues:
                    f.write(f"    {line}\n")
                f.write("\n")

        f.write("-" * 62 + "\n")
        f.write(f"  Clean tickers ({len(ok)}): {', '.join(ok) if ok else 'none'}\n")

    issue_count = sum(len(v) for _, v in issues)
    print(f"  → db_health.txt   ({len(issues)} tickers with issues, {issue_count} total flags)")


# ─────────────────────────────────────────────────────────────────────────────
# 2. DATA DOWNLOAD
# ─────────────────────────────────────────────────────────────────────────────

def safe_row(df, *candidates):
    for name in candidates:
        if name in df.index:
            return df.loc[name]
    return None

def download_ticker(symbol, years_back):
    print(f"  Downloading {symbol} ...", end=" ", flush=True)
    try:
        t = yf.Ticker(symbol)
        info  = t.info
        inc   = t.income_stmt
        bs    = t.balance_sheet
        cf    = t.cashflow
        hist  = t.history(period="max", interval="1mo")

        if inc.empty:
            print("FAILED (no income data)")
            return None

        dates = sorted(inc.columns)[-years_back:]
        years = [d.year for d in dates]

        shares_row = safe_row(inc,
            "Diluted Average Shares", "Basic Average Shares",
            "DilutedAverageShares",   "BasicAverageShares")

        def per_share(row, fallback=None):
            if row is None:
                return [None] * len(dates)
            vals = []
            for d in dates:
                try:
                    total  = float(row[d])
                    shares = float(shares_row[d]) if shares_row is not None else None
                    if shares and shares > 0:
                        vals.append(round(total / shares, 4))
                    elif fallback is not None:
                        vals.append(fallback)
                    else:
                        vals.append(None)
                except Exception:
                    vals.append(None)
            return vals

        eps_row = safe_row(inc,
            "Diluted EPS", "DilutedEPS", "Basic EPS", "BasicEPS")
        if eps_row is not None:
            eps = [round(float(eps_row[d]), 4) if d in eps_row.index and eps_row[d] is not None
                   else None for d in dates]
        else:
            ni_row = safe_row(inc, "Net Income", "NetIncome",
                              "Net Income Common Stockholders")
            eps = per_share(ni_row)

        prices = []
        for yr in years:
            mask = hist.index.year == yr
            sub  = hist[mask]
            if not sub.empty:
                prices.append(round(float(sub["Close"].iloc[-1]), 2))
            else:
                prices.append(None)

        pe = []
        for p, e in zip(prices, eps):
            if p is not None and e and e > 0:
                pe.append(round(p / e, 2))
            else:
                pe.append(None)

        ni_row  = safe_row(inc, "Net Income", "NetIncome",
                           "Net Income Common Stockholders")
        eq_row  = safe_row(bs,
            "Stockholders Equity", "StockholdersEquity",
            "Total Equity Gross Minority Interest",
            "Common Stock Equity")
        roe = []
        for d in dates:
            try:
                ni = float(ni_row[d])
                eq = float(eq_row[d])
                roe.append(round(ni / eq * 100, 2) if eq and eq != 0 else None)
            except Exception:
                roe.append(None)

        bvps = per_share(eq_row)

        debt_row   = safe_row(bs, "Total Debt", "TotalDebt",
                               "Long Term Debt", "LongTermDebt")
        assets_row = safe_row(bs, "Total Assets", "TotalAssets")
        debt_assets = []
        for d in dates:
            try:
                debt   = float(debt_row[d])
                assets = float(assets_row[d])
                debt_assets.append(round(debt / assets, 4) if assets else None)
            except Exception:
                debt_assets.append(None)

        ocf_row  = safe_row(cf,
            "Operating Cash Flow", "OperatingCashFlow",
            "Cash Flow From Continuing Operating Activities")
        capex_row = safe_row(cf,
            "Capital Expenditure", "CapitalExpenditure",
            "Purchase Of PPE", "PurchaseOfPPE")

        ocfps = per_share(ocf_row)
        fcfps = []
        for i2, d in enumerate(dates):
            try:
                ocf   = float(ocf_row[d])
                capex = float(capex_row[d]) if capex_row is not None else 0
                shares = float(shares_row[d]) if shares_row is not None else None
                if shares and shares > 0:
                    fcf = (ocf + capex) / shares
                    fcfps.append(round(fcf, 4))
                else:
                    fcfps.append(None)
            except Exception:
                fcfps.append(None)

        rev_row = safe_row(inc, "Total Revenue", "TotalRevenue", "Revenue")
        revps = per_share(rev_row)

        divs = t.dividends
        divps = []
        for yr in years:
            annual = divs[divs.index.year == yr].sum()
            divps.append(round(float(annual), 4))

        # ── 2026-06-19: new per-year fields ─────────────────────────────────

        # Gross margin % — Gross Profit / Total Revenue
        gp_row = safe_row(inc, "Gross Profit", "GrossProfit")
        gross_margin = []
        for d in dates:
            try:
                gp  = float(gp_row[d])
                rev = float(rev_row[d])
                gross_margin.append(round(gp / rev * 100, 2) if rev else None)
            except Exception:
                gross_margin.append(None)

        # Operating margin % — Operating Income / Total Revenue
        op_inc_row = safe_row(inc, "Operating Income", "OperatingIncome", "EBIT")
        op_margin = []
        for d in dates:
            try:
                oi  = float(op_inc_row[d])
                rev = float(rev_row[d])
                op_margin.append(round(oi / rev * 100, 2) if rev else None)
            except Exception:
                op_margin.append(None)

        # Net margin % — Net Income / Total Revenue  (mum's request)
        net_margin = []
        for d in dates:
            try:
                ni  = float(ni_row[d])
                rev = float(rev_row[d])
                net_margin.append(round(ni / rev * 100, 2) if rev else None)
            except Exception:
                net_margin.append(None)

        # FCF margin % — (OCF + CapEx) / Total Revenue
        fcf_margin = []
        for d in dates:
            try:
                ocf   = float(ocf_row[d])
                capex = float(capex_row[d]) if capex_row is not None else 0
                rev   = float(rev_row[d])
                fcf_margin.append(round((ocf + capex) / rev * 100, 2) if rev else None)
            except Exception:
                fcf_margin.append(None)

        # Shares outstanding per year — straight from the shares_row used
        # above for per-share calcs, stored separately for buyback-yield.
        shares_out = []
        for d in dates:
            try:
                shares_out.append(round(float(shares_row[d]), 0) if shares_row is not None else None)
            except Exception:
                shares_out.append(None)

        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        analyst_tp    = info.get("targetMeanPrice")
        analyst_low   = info.get("targetLowPrice")
        analyst_high  = info.get("targetHighPrice")
        consensus     = info.get("recommendationKey", "").replace("_", " ").title()
        trailing_pe   = info.get("trailingPE")
        forward_pe    = info.get("forwardPE")
        peg_ratio     = info.get("pegRatio")       # current PEG from yfinance
        short_float   = (
            info.get("shortPercentOfFloat") or
            info.get("sharesPercentSharesOut") or
            info.get("shortPercent")
        )

        # ── 2026-06-19: current-snapshot valuation/balance-sheet metrics ────
        # These are point-in-time (not historical series) since yfinance
        # doesn't expose clean historical EV or market cap per fiscal year.

        enterprise_value = info.get("enterpriseValue")
        ebitda           = info.get("ebitda")
        ev_ebitda = None
        if enterprise_value and ebitda and ebitda > 0:
            ev_ebitda = round(enterprise_value / ebitda, 2)

        total_debt_cur = info.get("totalDebt")
        total_cash_cur = info.get("totalCash")
        net_debt_ebitda = None
        if total_debt_cur is not None and total_cash_cur is not None and ebitda and ebitda > 0:
            net_debt = total_debt_cur - total_cash_cur
            net_debt_ebitda = round(net_debt / ebitda, 2)

        # Interest coverage — EBIT / interest expense, using most recent
        # fiscal year's income-statement figures (not the `info` snapshot,
        # since interest expense isn't reliably in `info`).
        interest_coverage = None
        try:
            int_exp_row = safe_row(inc, "Interest Expense", "InterestExpense")
            latest_date = dates[-1]
            ebit_latest = float(op_inc_row[latest_date])
            int_exp     = float(int_exp_row[latest_date]) if int_exp_row is not None else None
            if int_exp and int_exp != 0:
                interest_coverage = round(abs(ebit_latest / int_exp), 2)
        except Exception:
            interest_coverage = None

        # Buyback yield — % change in shares outstanding over the most
        # recent year, sign-flipped so a shrinking share count is positive
        # (i.e. "yield" to existing holders).
        buyback_yield = None
        try:
            valid_shares = [(y, s) for y, s in zip(years, shares_out) if s is not None]
            if len(valid_shares) >= 2:
                first_s, last_s = valid_shares[0][1], valid_shares[-1][1]
                if first_s and first_s > 0:
                    buyback_yield = round((first_s - last_s) / first_s * 100, 2)
        except Exception:
            buyback_yield = None

        # 2026-06-21: dividend_yield used to come straight from Yahoo's
        # info["dividendYield"], with a `< 1` check trying to guess whether
        # that number was a fraction (0.0072) or already a percent (0.72).
        # That guess is unfixable as written — a sub-1%-yield stock and a
        # fraction both land under 1, so they're numerically identical to
        # the heuristic. MSFT (~0.7% yield) is exactly that case: 0.72 reads
        # as "must be a fraction," gets multiplied by 100, and comes out as
        # a fake 72% yield. Whichever convention Yahoo happens to use this
        # week, the ambiguity is structural, not a caching issue.
        #
        # Sidestep it entirely — divps and current_price are both already
        # known in unambiguous units (dollars), so the yield can be derived
        # directly instead of trusting Yahoo's pre-formatted figure.
        dividend_yield = None
        try:
            latest_divps = divps[-1] if divps else None
            if latest_divps and current_price and current_price > 0:
                dividend_yield = round(latest_divps / current_price * 100, 2)
            else:
                # Best-effort fallback only — same ambiguity as before, but
                # better than nothing if divps/current_price aren't available.
                raw = info.get("dividendYield")
                if raw is not None:
                    dividend_yield = round(raw * 100, 2) if raw < 1 else round(raw, 2)
        except Exception:
            dividend_yield = None

        print("OK")
        return {
            "symbol":        symbol,
            "name":          info.get("longName", symbol),
            "years":         years,
            "prices":        prices,
            "eps":           eps,
            "pe":            pe,
            "roe":           roe,
            "bvps":          bvps,
            "debt_assets":   debt_assets,
            "ocfps":         ocfps,
            "fcfps":         fcfps,
            "revps":         revps,
            "divps":         divps,
            "gross_margin":  gross_margin,
            "op_margin":     op_margin,
            "net_margin":    net_margin,
            "fcf_margin":    fcf_margin,
            "shares_out":    shares_out,
            "current_price": current_price,
            "analyst_tp":    analyst_tp,
            "analyst_low":   analyst_low,
            "analyst_high":  analyst_high,
            "consensus":     consensus,
            "trailing_pe":   trailing_pe,
            "forward_pe":    forward_pe,
            "peg_ratio":     peg_ratio,
            "short_float":   short_float,
            "ev_ebitda":          ev_ebitda,
            "net_debt_ebitda":    net_debt_ebitda,
            "interest_coverage":  interest_coverage,
            "buyback_yield":      buyback_yield,
            "dividend_yield":     dividend_yield,
        }

    except Exception as e:
        print(f"FAILED ({e})")
        return None

def download_ticker_quarterly(symbol):
    """
    Pulls whatever quarters yfinance currently exposes live — typically the
    last ~4-5, sometimes fewer. Deliberately no quarters_back parameter:
    fetch always grabs the full live window every time, since there's no
    upside to asking for less than the max. How many quarters get
    *displayed* later is a separate decision, made against whatever's been
    accumulated in the DB across runs — see trim_to_quarters() and the
    start=/end= filter on load_quarterly_from_db().

    Network note: this can't be exercised against live Yahoo Finance data
    in a sandboxed environment without internet access to that domain —
    verify this against real data once it's running locally.
    """
    print(f"  Downloading {symbol} (quarterly) ...", end=" ", flush=True)
    try:
        t   = yf.Ticker(symbol)
        inc = t.quarterly_income_stmt
        bs  = t.quarterly_balance_sheet
        cf  = t.quarterly_cashflow

        if inc.empty:
            print("FAILED (no quarterly income data)")
            return None

        dates           = sorted(inc.columns)
        dates           = [d.tz_localize(None) if getattr(d, "tzinfo", None) is not None else d
                            for d in dates]
        quarter_ends    = [d.strftime("%Y-%m-%d") for d in dates]
        fiscal_years    = [d.year for d in dates]
        fiscal_quarters = [(d.month - 1) // 3 + 1 for d in dates]

        shares_row = safe_row(inc,
            "Diluted Average Shares", "Basic Average Shares",
            "DilutedAverageShares",   "BasicAverageShares")
        ni_row  = safe_row(inc, "Net Income", "NetIncome",
                            "Net Income Common Stockholders")
        rev_row = safe_row(inc, "Total Revenue", "TotalRevenue", "Revenue")

        # ── Actual EPS — prefer the reported line item, fall back to
        # net income / shares the same way download_ticker() does ─────────
        eps_row = safe_row(inc, "Diluted EPS", "DilutedEPS", "Basic EPS", "BasicEPS")
        eps_actual = []
        for d in dates:
            try:
                if eps_row is not None and eps_row[d] is not None:
                    eps_actual.append(round(float(eps_row[d]), 4))
                else:
                    ni = float(ni_row[d])
                    sh = float(shares_row[d]) if shares_row is not None else None
                    eps_actual.append(round(ni / sh, 4) if sh and sh > 0 else None)
            except Exception:
                eps_actual.append(None)

        # ── Revenue & net income, raw totals (panel 3 plots these as bars,
        # not per-share — unlike annual_data, which only ever stores
        # per-share figures) ────────────────────────────────────────────────
        revenue, net_income = [], []
        for d in dates:
            try:
                revenue.append(round(float(rev_row[d]), 0) if rev_row is not None else None)
            except Exception:
                revenue.append(None)
            try:
                net_income.append(round(float(ni_row[d]), 0) if ni_row is not None else None)
            except Exception:
                net_income.append(None)

        # ── Margins ──────────────────────────────────────────────────────────
        gp_row     = safe_row(inc, "Gross Profit", "GrossProfit")
        op_inc_row = safe_row(inc, "Operating Income", "OperatingIncome", "EBIT")
        gross_margin, op_margin, net_margin = [], [], []
        for d, rev in zip(dates, revenue):
            try:
                gross_margin.append(round(float(gp_row[d]) / rev * 100, 2)
                                     if gp_row is not None and rev else None)
            except Exception:
                gross_margin.append(None)
            try:
                op_margin.append(round(float(op_inc_row[d]) / rev * 100, 2)
                                  if op_inc_row is not None and rev else None)
            except Exception:
                op_margin.append(None)
        for ni, rev in zip(net_income, revenue):
            net_margin.append(round(ni / rev * 100, 2) if ni is not None and rev else None)

        # ── Balance sheet: debt & cash, raw totals (panel 5) ────────────────
        debt_row = safe_row(bs, "Total Debt", "TotalDebt",
                             "Long Term Debt", "LongTermDebt")
        cash_row = safe_row(bs, "Cash And Cash Equivalents", "CashAndCashEquivalents",
                             "Cash Cash Equivalents And Short Term Investments")
        debt, cash = [], []
        for d in dates:
            try:
                debt.append(round(float(debt_row[d]), 0) if debt_row is not None else None)
            except Exception:
                debt.append(None)
            try:
                cash.append(round(float(cash_row[d]), 0) if cash_row is not None else None)
            except Exception:
                cash.append(None)

        # ── Cash flow: OCF, CapEx, and FCF = OCF + CapEx (panel 4 needs OCF
        # and CapEx as two distinct series, not just the combined figure
        # panel 5 uses) ──────────────────────────────────────────────────
        ocf_row   = safe_row(cf, "Operating Cash Flow", "OperatingCashFlow",
                              "Cash Flow From Continuing Operating Activities")
        capex_row = safe_row(cf, "Capital Expenditure", "CapitalExpenditure",
                              "Purchase Of PPE", "PurchaseOfPPE")
        ocf, capex_list, fcf = [], [], []
        for d in dates:
            try:
                ocf.append(round(float(ocf_row[d]), 0))
            except Exception:
                ocf.append(None)
            try:
                capex_list.append(round(float(capex_row[d]), 0) if capex_row is not None else None)
            except Exception:
                capex_list.append(None)
        for o, c in zip(ocf, capex_list):
            try:
                fcf.append(round(o + (c or 0), 0) if o is not None else None)
            except Exception:
                fcf.append(None)

        # ── Quarter-end price + trailing P/E ────────────────────────────────
        # t.history() comes back with a tz-aware index (localized to the
        # exchange's timezone), but `dates` (quarterly_income_stmt columns)
        # and `report_date` (get_earnings_history index) are both tz-naive —
        # comparing the two directly raises "Invalid comparison between
        # dtype=datetime64[s, tz] and Timestamp". Strip the tz here, once,
        # rather than re-localizing every comparison below.
        hist = t.history(start=dates[0] - timedelta(days=10), interval="1d")
        if hist.index.tz is not None:
            hist.index = hist.index.tz_localize(None)
        prices = []
        for d in dates:
            sub = hist[hist.index <= d]
            prices.append(round(float(sub["Close"].iloc[-1]), 2) if not sub.empty else None)

        # Trailing P/E needs trailing-twelve-month EPS (this quarter plus
        # the 3 before it) in the denominator — dividing price by a single
        # quarter's EPS instead inflates the ratio ~4x versus the P/E
        # everyone else quotes. That needs 4 consecutive quarters of EPS
        # on file, so the earliest quarters won't have a P/E yet until
        # enough quarterly history has accumulated in the DB.
        pe = []
        for i, p in enumerate(prices):
            if p is None or i < 3:
                pe.append(None)
                continue
            ttm_window = eps_actual[i - 3:i + 1]
            if any(v is None for v in ttm_window):
                pe.append(None)
                continue
            ttm_eps = sum(ttm_window)
            pe.append(round(p / ttm_eps, 2) if ttm_eps > 0 else None)

        # ── EPS estimate, earnings date, and price reaction ─────────────────
        # get_earnings_history() is indexed by *report* date, not period-end,
        # and typically covers roughly the same ~4-5 quarter window as the
        # statements above but isn't guaranteed to line up 1:1. Align the
        # two lists by chronological order from the most recent end, rather
        # than assuming matching dates or matching counts.
        eps_estimate    = [None] * len(dates)
        earnings_dates  = [None] * len(dates)
        price_react_pct = [None] * len(dates)
        try:
            eh = t.get_earnings_history()
            if eh is not None and not eh.empty:
                eh = eh[eh["epsActual"].notna()].sort_index()
                n  = min(len(eh), len(dates))
                if n > 0:
                    eh_tail = eh.iloc[-n:]
                    offset  = len(dates) - n
                    for i, (report_date, row) in enumerate(eh_tail.iterrows()):
                        idx = offset + i
                        if getattr(report_date, "tzinfo", None) is not None:
                            report_date = report_date.tz_localize(None)
                        eps_estimate[idx]   = row.get("epsEstimate")
                        earnings_dates[idx] = report_date.strftime("%Y-%m-%d")
                        # Price reaction: close ~3 trading days after the
                        # report vs. the close on/just before the report —
                        # adjust the window here if you want a tighter
                        # (next-day) or wider (one-week) reaction read.
                        try:
                            window = hist[hist.index >= report_date]
                            if len(window) >= 4:
                                before = float(window["Close"].iloc[0])
                                after  = float(window["Close"].iloc[3])
                                price_react_pct[idx] = round((after - before) / before * 100, 2)
                        except Exception:
                            pass
        except Exception:
            pass

        print("OK")
        return {
            "symbol":          symbol,
            "quarter_ends":    quarter_ends,
            "fiscal_years":    fiscal_years,
            "fiscal_quarters": fiscal_quarters,
            "earnings_dates":  earnings_dates,
            "prices":          prices,
            "eps_actual":      eps_actual,
            "eps_estimate":    eps_estimate,
            "pe":              pe,
            "revenue":         revenue,
            "net_income":      net_income,
            "gross_margin":    gross_margin,
            "op_margin":       op_margin,
            "net_margin":      net_margin,
            "debt":            debt,
            "ocf":             ocf,
            "capex":           capex_list,
            "fcf":             fcf,
            "cash":            cash,
            "price_react_pct": price_react_pct,
        }

    except Exception as e:
        print(f"FAILED ({e})")
        return None

def download_etf(symbol, years_back):
    print(f"  Downloading {symbol} (ETF) ...", end=" ", flush=True)
    try:
        t    = yf.Ticker(symbol)
        info = t.info
        hist = t.history(period="max", interval="1mo", auto_adjust=True)
        divs = t.dividends

        if hist.empty:
            print("FAILED (no price data)")
            return None

        years = sorted(set(hist.index.year))[-years_back:]
        prices, distributions, annual_returns = [], [], []

        prev_price = None
        for yr in years:
            mask = hist.index.year == yr
            sub  = hist[mask]
            if not sub.empty:
                p = round(float(sub["Close"].iloc[-1]), 2)
                prices.append(p)
                ret = round((p / prev_price - 1) * 100, 2) if prev_price else None
                annual_returns.append(ret)
                prev_price = p
            else:
                prices.append(None)
                annual_returns.append(None)

            annual_div = divs[divs.index.year == yr].sum()
            distributions.append(round(float(annual_div), 4))

        print("OK")
        return {
            "symbol":        symbol,
            "name":          info.get("longName", symbol),
            "quote_type":    "ETF",
            "years":         years,
            "prices":        prices,
            "distributions": distributions,
            "annual_returns": annual_returns,
            "expense_ratio": info.get("annualReportExpenseRatio") or info.get("expenseRatio"),
            "aum":           info.get("totalAssets"),
            "category":      info.get("category", ""),
            "current_price": info.get("regularMarketPrice") or info.get("currentPrice"),
        }
    except Exception as e:
        print(f"FAILED ({e})")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def clean(arr):
    return [np.nan if (v is None or (isinstance(v, float) and np.isnan(v))) else v
            for v in arr]

def latest(arr):
    for v in reversed(arr):
        if v is not None and not np.isnan(v):
            return v
    return np.nan

def year_labels(years):
    return [str(y) for y in years]

def compute_historical_peg(pe_list, eps_list):
    """
    For each year, PEG = P/E ÷ year-on-year EPS growth (%).
    Looks back past None/zero years to find the last valid prior EPS,
    so a single missing year doesn't poison all subsequent data points.
    Returns None where growth is below 5% (PEG not meaningful at near-zero
    growth) or negative, or where no valid prior EPS exists.
    """
    result = []
    for i, (pe, eps) in enumerate(zip(pe_list, eps_list)):
        if i == 0 or pe is None or eps is None or eps <= 0:
            result.append(None)
            continue
        # Walk back to find the nearest valid prior EPS
        prev_eps = None
        for j in range(i - 1, -1, -1):
            if eps_list[j] is not None and eps_list[j] > 0:
                prev_eps = eps_list[j]
                break
        if prev_eps is None:
            result.append(None)
            continue
        growth_pct = ((eps / prev_eps) - 1) * 100
        if growth_pct <= 5:
            result.append(None)
            continue
        result.append(round(pe / growth_pct, 4))
    return result

def add_zero_line(ax):
    ax.axhline(0, color="#ccc", linewidth=0.8, zorder=0)


# ─────────────────────────────────────────────────────────────────────────────
# 3a. CAGR / MARGIN HELPERS  (2026-06-19)
# ─────────────────────────────────────────────────────────────────────────────
# Single shared implementation used by: chart panel titles, footer summary
# line, and (duplicated, by design — see interactive_table.py header note)
# the scorecard tables.

def cagr_pct(values, n_years):
    """
    Fixed-window CAGR over the most recent n_years. Returns None if there
    isn't n_years+1 of clean history. Mirrors cagr_full_window's handling
    of negative/zero-crossing values: same-sign-negative uses a sign-aware
    magnitude rate, and a zero-crossing window gets shifted positive using
    the same fixed, data-derived shift trick (here min(start, end) is the
    relevant minimum, since this function only ever sees those two points).
    """
    clean_vals = [v for v in values if v is not None]
    if len(clean_vals) < n_years + 1:
        return None

    end, start = clean_vals[-1], clean_vals[-(n_years + 1)]
    if start == 0 or end == 0:
        return None

    if start > 0 and end > 0:
        return round(((end / start) ** (1 / n_years) - 1) * 100, 2)

    if start < 0 and end < 0:
        mag_rate = ((abs(end) / abs(start)) ** (1 / n_years) - 1) * 100
        return round(-mag_rate, 2)

    # crossed zero — same shift approach as cagr_full_window
    shift = abs(min(start, end)) + 1
    shifted_start, shifted_end = start + shift, end + shift
    if shifted_start <= 0:
        return None
    return round(((shifted_end / shifted_start) ** (1 / n_years) - 1) * 100, 2)


def cagr_full_window(values, years_list):
    """
    Full-window CAGR — from the first valid (year, value) pair to the last
    in the given window, regardless of how many years that window spans.
    This is the "always start of search window to end" version used for
    chart panel headlines and the footer summary line, since those should
    describe exactly what's plotted on screen.

    Three cases:
    - start & end both positive: standard CAGR.
    - start & end both negative: sign-aware magnitude CAGR (negative =
      got worse / more negative, positive = improved / less negative).
    - sign crossed zero (start neg, end pos, or vice versa): standard
      CAGR is undefined here (no real rate solves it), so this uses a
      fixed, deterministic shift — every value in the window is shifted
      up by (abs(min value in window) + 1) so the whole series becomes
      positive, then standard CAGR runs on the shifted series. The shift
      amount is derived from the data itself (not a free parameter you'd
      have to pick by hand), so the same series always gets the same
      shift and the same answer. This number is NOT a true compound
      growth rate (compounding it won't reproduce the original values —
      shifting breaks that property for negative-crossing data, there is
      no way around that), but it reliably reads positive when the
      metric is improving and negative when it's worsening, which is
      what this is actually used for on this chart.
    """
    pairs = [(y, v) for y, v in zip(years_list, values) if v is not None]
    if len(pairs) < 2:
        return None
    n = pairs[-1][0] - pairs[0][0]
    if n <= 0:
        return None

    start, end = pairs[0][1], pairs[-1][1]
    if start == 0 or end == 0:
        return None  # can't compute a meaningful rate from/to exactly zero

    if start > 0 and end > 0:
        return round(((end / start) ** (1 / n) - 1) * 100, 1)

    if start < 0 and end < 0:
        # Sign-aware magnitude CAGR: the raw rate of |value| growth is
        # positive when the magnitude is growing (i.e. getting further
        # from zero / worse) and negative when shrinking (improving).
        # Negate it so the sign reads the way these stats normally do:
        # negative = worsening, positive = improving.
        mag_rate = ((abs(end) / abs(start)) ** (1 / n) - 1) * 100
        return round(-mag_rate, 1)

    # Crossed zero (start and end have opposite signs). Shift the whole
    # window up by a fixed, data-derived amount so start/end both become
    # positive, then run standard CAGR on the shifted pair.
    all_vals = [v for _, v in pairs]
    shift = abs(min(all_vals)) + 1
    shifted_start, shifted_end = start + shift, end + shift
    if shifted_start <= 0:  # guard, shouldn't trigger given the +1 above
        return None
    return round(((shifted_end / shifted_start) ** (1 / n) - 1) * 100, 1)


def margin_trend(margin_list):
    """Latest margin minus earliest available margin in the window (pp)."""
    clean_vals = [v for v in margin_list if v is not None]
    if len(clean_vals) < 2:
        return None
    return round(clean_vals[-1] - clean_vals[0], 2)


def fmt_pct(val, decimals=1):
    return f"{val:+.{decimals}f}%" if val is not None else "N/A"


def fmt_x(val, decimals=2):
    return f"{val:.{decimals}f}x" if val is not None else "N/A"


def _money_tick(val, pos=None):
    """Adaptive $ tick label — B/M/K picked by magnitude so it reads right
    whether the series is mega-cap revenue (billions) or a smaller
    ticker's (millions)."""
    av = abs(val)
    if av >= 1e9:
        return f"${val/1e9:,.1f}B"
    if av >= 1e6:
        return f"${val/1e6:,.0f}M"
    if av >= 1e3:
        return f"${val/1e3:,.0f}K"
    return f"${val:,.0f}"


def apply_money_axis(ax):
    """Swap in _money_tick for the y-axis's default ScalarFormatter.

    matplotlib's own fix for big numbers is a 'x1e10' offset label parked
    at the top-left of the axes — but the in-axes legend's loc="best"
    only avoids overlapping plotted bars/lines, it has no idea that
    offset text is sitting there too, so on a panel where the data is
    short on the left (which is exactly when "best" picks upper-left)
    the legend lands right on top of it. Giving every tick its own
    self-contained "$70.1B" label removes the offset text entirely, so
    there's nothing left for the legend to collide with.
    """
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_money_tick))


# ─────────────────────────────────────────────────────────────────────────────
# 3b. TRIM HELPER
# ─────────────────────────────────────────────────────────────────────────────
#
# Called immediately after DB load so every downstream plot/table/calculation
# sees only the years the user asked for.  The DB always stores the full
# history; trimming is a display-time operation only.
#
# Without this, a ticker with 30 years in the DB would pollute:
#   • chart x-axes (showing 30 years when user asked for 11)
#   • total_return, best/worst year, avg_return, volatility (all use full list)
#   • EPS CAGR (start year would be 19 years earlier than intended)

STOCK_LIST_FIELDS = [
    "years", "prices", "eps", "pe", "roe", "bvps",
    "debt_assets", "ocfps", "fcfps", "revps", "divps",
    "gross_margin", "op_margin", "net_margin", "fcf_margin", "shares_out",
]
ETF_LIST_FIELDS = ["years", "prices", "distributions", "annual_returns"]

# All quarter-aligned lists in the dict returned by load_quarterly_from_db()/
# download_ticker_quarterly() — kept as one named constant so trim_to_quarters
# and anything else that needs to walk "every quarterly series at once"
# (chart code, exports) all stay in sync if a field gets added later.
QUARTERLY_LIST_FIELDS = [
    "quarter_ends", "fiscal_years", "fiscal_quarters", "earnings_dates",
    "prices", "eps_actual", "eps_estimate", "pe", "revenue", "net_income",
    "gross_margin", "op_margin", "net_margin", "debt", "ocf", "capex", "fcf", "cash",
    "price_react_pct",
]


def trim_to_years(d: dict, years_back: int) -> dict:
    """
    Return a shallow copy of d with every year-aligned list sliced to
    the most recent `years_back` entries.  Non-list fields are untouched.
    """
    trimmed = dict(d)
    fields = ETF_LIST_FIELDS if d.get("quote_type") == "ETF" else STOCK_LIST_FIELDS
    for f in fields:
        if f in trimmed and isinstance(trimmed[f], list):
            trimmed[f] = trimmed[f][-years_back:]
    return trimmed


def trim_to_quarters(d: dict, quarters_back: int) -> dict:
    """
    Quarterly twin of trim_to_years() — this is the "last N quarters"
    display mode. The DB itself may hold years' worth of accumulated
    history; this just slices what gets shown/charted, same way years_back
    slices annual_data without ever touching what's actually stored.

    For the other display mode — an explicit date range like "Q1 2025 to
    Q4 2026" — don't use this at all; pass start=/end= straight into
    load_quarterly_from_db() instead, since that's a DB-level filter rather
    than a slice of an already-loaded dict.
    """
    trimmed = dict(d)
    for f in QUARTERLY_LIST_FIELDS:
        if f in trimmed and isinstance(trimmed[f], list):
            trimmed[f] = trimmed[f][-quarters_back:]
    return trimmed


def quarter_label(quarter_end: str, fiscal_quarter=None, fiscal_year=None) -> str:
    """
    Human-readable axis label for a quarter_end date, e.g. "Q1 '25".
    Falls back to deriving fiscal_quarter/fiscal_year from the date itself
    (calendar-quarter approximation) if they weren't stored — good enough
    for an axis label even though it may not match a company's actual
    fiscal calendar for display purposes elsewhere.
    """
    d = dateutil.parser.parse(quarter_end)
    if fiscal_quarter is None:
        fiscal_quarter = (d.month - 1) // 3 + 1
    if fiscal_year is None:
        fiscal_year = d.year
    return f"Q{fiscal_quarter} '{str(fiscal_year)[-2:]}"



# ─────────────────────────────────────────────────────────────────────────────
# 4. PLOT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

STYLE = {
    "font.family":       "monospace",
    "axes.spines.top":   False,
    "axes.spines.right": False,
    "axes.grid":         True,
    "grid.color":        "#f0f0f0",
    "grid.linewidth":    0.8,
    "axes.facecolor":    "white",
    "figure.facecolor":  "white",
    "patch.linewidth":   0,
    "patch.edgecolor":   "none",
}

# ── Chart font scale (Edit ▸ Preferences ▸ Display Settings…) ────────────────
# Re-read each run so changes made between clicks of "Go" take effect without
# needing to restart the app. Every explicit fontsize=/labelsize= literal in
# the plotting code below is wrapped in fs(...) so the whole chart set scales
# together, the same way the ticker picker and scorecard windows do.
_CHART_FONT_SCALE = 110.0

def refresh_chart_font_scale():
    global _CHART_FONT_SCALE
    _settings = app_settings.load_settings()
    _CHART_FONT_SCALE = app_settings.get_float(_settings, "chart_font_scale", 110.0)
    return _CHART_FONT_SCALE

def fs(base_size, minimum=6):
    """Scale a base matplotlib font size by the user's chart-font preference."""
    return app_settings.scaled_size(base_size, _CHART_FONT_SCALE, minimum)

def set_panel_title(ax, name, stat=None, base_size=10):
    """
    Set a panel title as two stacked lines: name on top, stat below,
    inside ONE Text object (via "\\n"). A single long line
    ("Name   ·   STAT +xx.x%") can overflow a narrow panel's width and
    collide with the neighboring panel's title — splitting it into two
    shorter lines keeps each one short enough to fit regardless of how
    long the stat string gets. Using one Text object (vs. a separate
    floating text element) matters because constrained_layout measures
    the title's bounding box to reserve row height — a second, separate
    text element above/below it is invisible to that measurement and
    will get clipped or overlap neighboring rows.
    """
    title_str = f"{name}\n{stat}" if stat else name
    ax.set_title(title_str, fontsize=fs(base_size), fontweight="bold",
                 linespacing=1.4)

def apply_style():
    refresh_chart_font_scale()
    STYLE["font.size"] = fs(9)
    plt.rcParams.update(STYLE)

def ticker_legend(ax, data_list, colors):
    handles = [Line2D([0],[0], color=c, linewidth=2, label=d["symbol"])
               for d, c in zip(data_list, colors)]
    ax.legend(handles=handles, fontsize=fs(8), framealpha=0.9,
              loc="upper left", ncol=max(1, len(data_list)//5))


# ─────────────────────────────────────────────────────────────────────────────
# 4b. PER-PANEL DATA TABLE  (2026-06-19)
# ─────────────────────────────────────────────────────────────────────────────
# Small table placed directly under a chart panel, one row per series shown
# in that panel, one column per year matching the chart's x-axis. Row label
# colours match each series' chart colour so it reads the same way the
# Stock Scorecard tables already do.

def _draw_panel_table(ax_table, yrs, series_rows):
    """
    ax_table     : the matplotlib Axes to draw the table into (already
                   sized/positioned via nested GridSpec — this function
                   just turns its axis off and fills it with a table)
    yrs          : list of year label strings, same order as the chart
    series_rows  : list of (row_label, color, values, fmt_fn) tuples
                   values must be the same length as yrs (None allowed)
    """
    ax_table.axis("off")
    if not series_rows:
        return

    n_cols = len(yrs)
    n_rows = len(series_rows)

    cell_text   = []
    row_labels  = []
    row_colors  = []

    for label, color, values, fmt_fn in series_rows:
        row_labels.append(label)
        row_colors.append(color)
        row = []
        for v in values:
            if v is None or (isinstance(v, float) and np.isnan(v)):
                row.append("—")
            else:
                row.append(fmt_fn(v))
        cell_text.append(row)

    tbl = ax_table.table(
        cellText=cell_text,
        rowLabels=row_labels,
        colLabels=None,        # years already shown on the chart's x-axis above
        cellLoc="center",
        rowLoc="right",
        loc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(fs(7))
    tbl.scale(1.0, 1.3)

    for (row_idx, col_idx), cell in tbl.get_celld().items():
        cell.set_edgecolor("#E0E0E0")
        cell.set_linewidth(0.5)
        if col_idx == -1:
            # Row-label cell — colour the text to match the series
            color = row_colors[row_idx] if row_idx < len(row_colors) else "#333"
            cell.set_text_props(color=color, fontweight="bold")
            cell.set_facecolor("#FAFAFA")
        else:
            cell.set_facecolor("white" if row_idx % 2 == 0 else "#F7F9FC")


def _make_panel_gridspec(fig, outer_gs_cell, height_ratio=(5, 1)):
    """
    Splits one outer GridSpec cell into a chart sub-axis (top) and a table
    sub-axis (bottom), via a nested GridSpecFromSubplotSpec. Returns
    (chart_ax, table_ax).
    """
    inner = gridspec.GridSpecFromSubplotSpec(
        2, 1, subplot_spec=outer_gs_cell,
        height_ratios=list(height_ratio), hspace=0.04,
    )
    chart_ax = fig.add_subplot(inner[0])
    table_ax = fig.add_subplot(inner[1])
    return chart_ax, table_ax


# ─────────────────────────────────────────────────────────────────────────────
# 5. INDIVIDUAL-TICKER CHARTS
# ─────────────────────────────────────────────────────────────────────────────

def _draw_series(ax, yrs, x, vals, style, color, alpha=0.50, label="", marker="o", linewidth=2):
    """Draw bars or line depending on style string ('bar' or 'line')."""
    if style == "bar":
        ax.bar(yrs, clean(vals), color=color, alpha=alpha,
               linewidth=0, edgecolor="none", label=label)
    else:
        ax.plot(yrs, clean(vals), color=color, linewidth=linewidth,
                marker=marker, markersize=4, label=label)


def plot_single_ticker(d, color, prefs=None):
    if prefs is None:
        prefs = load_chart_prefs()

    apply_style()
    year_range = f"{d['years'][0]}–{d['years'][-1]}" if d.get("years") else ""
    fig = plt.figure(figsize=(16, 11), facecolor="white", dpi=100, layout="constrained")
    fig.suptitle(f"{d['symbol']} — {d['name']}  |  Fundamentals {year_range}",
                 fontsize=fs(14), fontweight="bold")

    # Outer grid: 2 rows of panels + 1 thin footer row, all real GridSpec
    # rows so layout="constrained" reserves space for the footer too.
    # (fig.text() at a fixed y= would NOT be seen by constrained layout —
    # it'd float at a fixed fraction and collide with panels on resize.)
    gs  = gridspec.GridSpec(3, 3, figure=fig, hspace=0.22, wspace=0.40,
                             height_ratios=[1, 1, 0.18])
    yrs = year_labels(d["years"])
    x   = np.arange(len(yrs))

    # Pre-compute all full-window CAGRs used in panel titles + footer
    price_cagr = cagr_full_window(d["prices"], d["years"])
    eps_cagr_v = cagr_full_window(d["eps"], d["years"])
    bvps_cagr  = cagr_full_window(d["bvps"], d["years"])
    fcf_cagr   = cagr_full_window(d.get("fcfps", []), d["years"])
    rev_cagr   = cagr_full_window(d.get("revps", []), d["years"])
    roe_avg_full = round(np.nanmean([v for v in d["roe"] if v is not None]), 1) \
                   if any(v is not None for v in d["roe"]) else None

    # ── Panel 1: Share Price ────────────────────────────────────────────────
    ax1, tbl1 = _make_panel_gridspec(fig, gs[0, 0])
    ax1.plot(yrs, clean(d["prices"]), color=color, linewidth=2, marker="o", markersize=4)
    if d.get("current_price"):
        ax1.axhline(d["current_price"], color=color, linestyle="--", linewidth=1,
                    label=f"Current ${d['current_price']:,.2f}")
    if d.get("analyst_tp"):
        ax1.axhline(d["analyst_tp"], color="#888", linestyle=":", linewidth=1,
                    label=f"TP ${d['analyst_tp']:,.2f}")
    title1_stat = f"CAGR {price_cagr:+.1f}%" if price_cagr is not None else None
    set_panel_title(ax1, "Share Price ($)", title1_stat)
    ax1.set_xticks(x[::2]); ax1.set_xticklabels(yrs[::2], fontsize=fs(8))
    ax1.legend(fontsize=fs(7))
    add_zero_line(ax1)
    _draw_panel_table(tbl1, yrs, [
        ("Price ($)", color, d["prices"], lambda v: f"${v:,.0f}"),
    ])

    # ── Panel 2: P/E & PEG ───────────────────────────────────────────────────
    hist_peg = compute_historical_peg(d["pe"], d["eps"])
    s2_pe  = prefs.get("panel2_pe",  "bar")
    s2_peg = prefs.get("panel2_peg", "line")

    ax2, tbl2 = _make_panel_gridspec(fig, gs[0, 1])
    ax2.set_axisbelow(True)
    ax2b = ax2.twinx()
    ax2b.set_axisbelow(True)
    ax2b.grid(False)

    _draw_series(ax2, yrs, x, d["pe"], s2_pe, color, alpha=0.50, label="P/E")
    _draw_series(ax2b, yrs, x, hist_peg, s2_peg, "#F59E0B", alpha=0.60, label="PEG", marker="s")

    fwd_pe  = d.get("forward_pe")
    cur_peg = d.get("peg_ratio")
    legend_lines = [
        Line2D([0],[0], color=color, linewidth=6 if s2_pe=="bar" else 2,
               alpha=0.75 if s2_pe=="bar" else 1.0, label="P/E"),
        Line2D([0],[0], color="#F59E0B", linewidth=6 if s2_peg=="bar" else 2,
               alpha=0.75 if s2_peg=="bar" else 1.0, label="PEG"),
    ]
    if fwd_pe:
        ax2.axhline(fwd_pe, color="#B45309", linestyle="--", linewidth=1.2,
                    label=f"Fwd P/E {fwd_pe:.1f}x")
        legend_lines.append(Line2D([0],[0], color="#B45309", linestyle="--",
                                   linewidth=1.2, label=f"Fwd P/E {fwd_pe:.1f}x"))
    if cur_peg:
        ax2b.axhline(cur_peg, color="#EF4444", linestyle=":", linewidth=1.4,
                     label=f"Cur PEG {cur_peg:.2f}")
        legend_lines.append(Line2D([0],[0], color="#EF4444", linestyle=":",
                                   linewidth=1.4, label=f"Cur PEG {cur_peg:.2f}"))

    ax2b.axhline(0, color="#ccc", linewidth=0.8, zorder=0)
    avg_pe_5 = None
    pe_clean = [v for v in d["pe"] if v is not None]
    if pe_clean:
        avg_pe_5 = round(sum(pe_clean[-5:]) / len(pe_clean[-5:]), 1)
    title2_stat = f"Fwd/Avg {fwd_pe/avg_pe_5:.2f}x" if (fwd_pe and avg_pe_5) else None
    set_panel_title(ax2, "P/E Ratio & PEG", title2_stat)
    ax2.set_xticks(x[::2]); ax2.set_xticklabels(yrs[::2], fontsize=fs(8))
    ax2.tick_params(axis="y", labelsize=fs(8))
    ax2b.tick_params(axis="y", labelsize=fs(8), colors="#F59E0B")
    ax2b.set_ylabel("PEG", fontsize=fs(8), color="#F59E0B")
    ax2.legend(handles=legend_lines, fontsize=fs(7))
    add_zero_line(ax2)
    _draw_panel_table(tbl2, yrs, [
        ("P/E", color, d["pe"], lambda v: f"{v:.1f}x"),
        ("PEG", "#F59E0B", hist_peg, lambda v: f"{v:.2f}"),
    ])

    # ── Panel 3: EPS & ROE ───────────────────────────────────────────────────
    s3_eps = prefs.get("panel3_eps", "bar")
    s3_roe = prefs.get("panel3_roe", "line")

    ax3, tbl3 = _make_panel_gridspec(fig, gs[0, 2])
    ax3.set_axisbelow(True)
    ax3b = ax3.twinx()
    ax3b.set_axisbelow(True)
    ax3b.grid(False)

    _draw_series(ax3, yrs, x, d["eps"], s3_eps, color, alpha=0.50, label="EPS ($)")
    _draw_series(ax3b, yrs, x, d["roe"], s3_roe, "#EF4444", alpha=0.60, label="ROE (%)", marker="s")

    title3_stat = f"EPS CAGR {eps_cagr_v:+.1f}%" if eps_cagr_v is not None else None
    set_panel_title(ax3, "EPS ($) & ROE (%)", title3_stat)
    ax3.set_xticks(x[::2]); ax3.set_xticklabels(yrs[::2], fontsize=fs(8))
    ax3.tick_params(axis="y", labelsize=fs(8))
    ax3b.tick_params(axis="y", labelsize=fs(8), colors="#EF4444")
    ax3b.set_ylabel("ROE %", fontsize=fs(8), color="#EF4444")
    lines3 = [
        Line2D([0],[0], color=color, linewidth=6 if s3_eps=="bar" else 2,
               alpha=0.75 if s3_eps=="bar" else 1.0, label="EPS ($)"),
        Line2D([0],[0], color="#EF4444", linewidth=6 if s3_roe=="bar" else 2,
               alpha=0.75 if s3_roe=="bar" else 1.0, label="ROE (%)"),
    ]
    ax3.legend(handles=lines3, fontsize=fs(7))
    add_zero_line(ax3)
    _draw_panel_table(tbl3, yrs, [
        ("EPS ($)", color, d["eps"], lambda v: f"${v:.2f}"),
        ("ROE (%)", "#EF4444", d["roe"], lambda v: f"{v:.1f}%"),
    ])

    # ── Panel 4: Book Value/Share & Debt/Assets ───────────────────────────────
    s4_bvps = prefs.get("panel4_bvps", "bar")
    s4_debt = prefs.get("panel4_debt", "line")
    da_pct = [v * 100 if v is not None else None for v in d["debt_assets"]]

    ax4, tbl4 = _make_panel_gridspec(fig, gs[1, 0])
    ax4.set_axisbelow(True)
    ax4b = ax4.twinx()
    ax4b.set_axisbelow(True)
    ax4b.grid(False)

    _draw_series(ax4, yrs, x, d["bvps"], s4_bvps, color, alpha=0.50, label="BV/Sh ($)")
    _draw_series(ax4b, yrs, x, da_pct, s4_debt, "#06B6D4", alpha=0.60, label="Debt/Assets (%)")

    title4_stat = f"BVPS CAGR {bvps_cagr:+.1f}%" if bvps_cagr is not None else None
    set_panel_title(ax4, "Book Value/Share & Debt/Assets", title4_stat)
    ax4.set_xticks(x[::2]); ax4.set_xticklabels(yrs[::2], fontsize=fs(8))
    ax4.tick_params(axis="y", labelsize=fs(8))
    ax4b.tick_params(axis="y", labelsize=fs(8), colors="#06B6D4")
    ax4b.set_ylabel("Debt/Assets %", fontsize=fs(8), color="#06B6D4")
    lines4 = [
        Line2D([0],[0], color=color, linewidth=6 if s4_bvps=="bar" else 2,
               alpha=0.75 if s4_bvps=="bar" else 1.0, label="BV/Sh ($)"),
        Line2D([0],[0], color="#06B6D4", linewidth=6 if s4_debt=="bar" else 2,
               alpha=0.75 if s4_debt=="bar" else 1.0, label="Debt/Assets (%)"),
    ]
    ax4.legend(handles=lines4, fontsize=fs(7))
    add_zero_line(ax4)
    _draw_panel_table(tbl4, yrs, [
        ("BV/Sh ($)", color, d["bvps"], lambda v: f"${v:.2f}"),
        ("Debt/Assets", "#06B6D4", da_pct, lambda v: f"{v:.1f}%"),
    ])

    # ── Panel 5: OCF/Share & FCF/Share ───────────────────────────────────────
    s5_ocf = prefs.get("panel5_ocf", "bar")
    s5_fcf = prefs.get("panel5_fcf", "line")

    ax5, tbl5 = _make_panel_gridspec(fig, gs[1, 1])
    ax5.set_axisbelow(True)
    _draw_series(ax5, yrs, x, d["ocfps"], s5_ocf, color, alpha=0.50, label="OCF/Sh ($)")
    _draw_series(ax5, yrs, x, d["fcfps"], s5_fcf, "#10B981", alpha=0.90, label="FCF/Sh ($)", marker="o", linewidth=2.5)
    title5_stat = f"FCF CAGR {fcf_cagr:+.1f}%" if fcf_cagr is not None else None
    set_panel_title(ax5, "OCF/Share & FCF/Share ($)", title5_stat)
    ax5.set_xticks(x[::2]); ax5.set_xticklabels(yrs[::2], fontsize=fs(8))
    ax5.tick_params(axis="y", labelsize=fs(8))
    ax5.legend(fontsize=fs(7))
    add_zero_line(ax5)
    _draw_panel_table(tbl5, yrs, [
        ("OCF/Sh ($)", color, d["ocfps"], lambda v: f"${v:.2f}"),
        ("FCF/Sh ($)", "#10B981", d["fcfps"], lambda v: f"${v:.2f}"),
    ])

    # ── Panel 6: Revenue/Share & Div/Share ───────────────────────────────────
    s6_rev = prefs.get("panel6_rev", "bar")
    s6_div = prefs.get("panel6_div", "line")

    ax6, tbl6 = _make_panel_gridspec(fig, gs[1, 2])
    ax6.set_axisbelow(True)
    ax6b = ax6.twinx()
    ax6b.set_axisbelow(True)
    ax6b.grid(False)

    _draw_series(ax6, yrs, x, d["revps"], s6_rev, color, alpha=0.50, label="Rev/Sh ($)")
    has_divs = any(v and v > 0 for v in d["divps"])
    if has_divs:
        _draw_series(ax6b, yrs, x, d["divps"], s6_div, "#7C3AED", alpha=0.80, label="Div/Sh ($)", marker="D")
        ax6b.tick_params(axis="y", labelsize=fs(8), colors="#8B5CF6")
        ax6b.set_ylabel("Div/Sh ($)", fontsize=fs(8), color="#8B5CF6")
    title6_stat = f"Rev/Sh CAGR {rev_cagr:+.1f}%" if rev_cagr is not None else None
    set_panel_title(ax6, "Revenue/Share & Div/Share ($)", title6_stat)
    ax6.set_xticks(x[::2]); ax6.set_xticklabels(yrs[::2], fontsize=fs(8))
    ax6.tick_params(axis="y", labelsize=fs(8))
    lines6 = [
        Line2D([0],[0], color=color, linewidth=6 if s6_rev=="bar" else 2,
               alpha=0.75 if s6_rev=="bar" else 1.0, label="Rev/Sh ($)"),
        Line2D([0],[0], color="#8B5CF6", linewidth=6 if s6_div=="bar" else 2,
               alpha=0.75 if s6_div=="bar" else 1.0, label="Div/Sh ($)"),
    ]
    ax6.legend(handles=lines6, fontsize=fs(7))
    add_zero_line(ax6)
    table6_rows = [("Rev/Sh ($)", color, d["revps"], lambda v: f"${v:.2f}")]
    if has_divs:
        table6_rows.append(("Div/Sh ($)", "#7C3AED", d["divps"], lambda v: f"${v:.2f}"))
    _draw_panel_table(tbl6, yrs, table6_rows)

    # ── Footer — full-window summary line, in its own GridSpec row ──────────
    consensus_str = d.get("consensus", "")
    tp_str  = f"  TP ${d['analyst_tp']:,.2f}" if d.get("analyst_tp") else ""
    low_str = f"  Low ${d['analyst_low']:,.2f}" if d.get("analyst_low") else ""
    hi_str  = f"  High ${d['analyst_high']:,.2f}" if d.get("analyst_high") else ""
    peg_str = f"  PEG {cur_peg:.2f}" if cur_peg else ""

    summary_parts = []
    if price_cagr is not None:
        summary_parts.append(f"Price CAGR: {price_cagr:+.1f}%")
    if rev_cagr is not None:
        summary_parts.append(f"Rev CAGR: {rev_cagr:+.1f}%")
    if eps_cagr_v is not None:
        summary_parts.append(f"EPS CAGR: {eps_cagr_v:+.1f}%")
    if fcf_cagr is not None:
        summary_parts.append(f"FCF CAGR: {fcf_cagr:+.1f}%")
    if roe_avg_full is not None:
        summary_parts.append(f"ROE avg: {roe_avg_full:.1f}%")
    if fwd_pe and avg_pe_5:
        summary_parts.append(f"Fwd PE vs Hist: {fwd_pe/avg_pe_5:.2f}x")

    ax_footer = fig.add_subplot(gs[2, :])
    ax_footer.axis("off")
    ax_footer.text(0.5, 0.75,
             f"Analyst Consensus: {consensus_str}{tp_str}{low_str}{hi_str}{peg_str}",
             ha="center", va="center", transform=ax_footer.transAxes,
             fontsize=fs(9), color="#555", fontfamily="monospace")
    if summary_parts:
        ax_footer.text(0.5, 0.15, "  |  ".join(summary_parts),
                 ha="center", va="center", transform=ax_footer.transAxes,
                 fontsize=fs(9), fontweight="bold",
                 color="#1A1A2E", fontfamily="monospace")

    return fig


def eps_surprise_pct(actual_list, estimate_list):
    """
    Per-quarter EPS surprise %: (actual - estimate) / |estimate| * 100.
    None where either side is missing or the estimate is exactly zero
    (a zero estimate makes the % undefined, not just large).
    """
    out = []
    for ea, ee in zip(actual_list, estimate_list):
        if ea is None or ee is None or ee == 0:
            out.append(None)
        else:
            out.append((ea - ee) / abs(ee) * 100.0)
    return out


def yoy_pct(values):
    """
    Quarter-over-same-quarter-last-year %, by index position (4 quarters
    back). Returns None for the first 4 entries, or wherever either side
    is missing/zero — same-quarter-last-year data only exists once the DB
    has accumulated 5+ quarters for that symbol (download_ticker_quarterly
    only ever pulls the live ~4-5 quarter window from yfinance per run, so
    deeper history builds up over time via the append-only upsert).
    """
    out = [None] * len(values)
    for i in range(4, len(values)):
        cur, prior = values[i], values[i - 4]
        if cur is None or prior is None or prior == 0:
            continue
        out[i] = (cur - prior) / abs(prior) * 100.0
    return out


def plot_quarterly_pulse(d, color):
    """
    The six-panel quarterly figure. `d` is whatever load_quarterly_from_db()
    /trim_to_quarters() returns, with one addition the caller needs to make
    first: d["name"] and d["symbol"] should be set (quarterly_data itself
    has no name column — that lives only in the `tickers` table — so main()
    should copy it over from the already-loaded annual dict before calling
    this, the same way plot_single_ticker's `d` already carries it).
    """
    apply_style()
    q_labels = [quarter_label(qe, fq, fy) for qe, fq, fy
                in zip(d["quarter_ends"], d["fiscal_quarters"], d["fiscal_years"])]
    x = np.arange(len(q_labels))
    q_range = f"{q_labels[0]}–{q_labels[-1]}" if q_labels else ""

    surprise_pct = eps_surprise_pct(d["eps_actual"], d["eps_estimate"])

    fig = plt.figure(figsize=(16, 11), facecolor="white", dpi=100, layout="constrained")
    fig.suptitle(f"{d.get('symbol','')} — {d.get('name','')}  |  Quarterly Pulse {q_range}",
                 fontsize=fs(14), fontweight="bold")

    gs = gridspec.GridSpec(3, 3, figure=fig, hspace=0.22, wspace=0.40,
                            height_ratios=[1, 1, 0.18])

    # ── Panel 1: Price vs EPS Beat/Miss ──────────────────────────────────────
    # Quarter-end close as a line, each point colour-coded by whether that
    # quarter's EPS beat, missed, or had no estimate to compare against —
    # so the panel reads as "did price actually agree with the earnings
    # result" rather than the isolated reaction-% bar it used to be. Panel 2
    # still shows actual vs. estimate EPS directly.
    ax1, tbl1 = _make_panel_gridspec(fig, gs[0, 0])
    ax1.set_axisbelow(True)
    bar_colors = []
    for ea, ee in zip(d["eps_actual"], d["eps_estimate"]):
        if ea is None or ee is None:
            bar_colors.append("#9CA3AF")     # no estimate to compare against
        elif ea >= ee:
            bar_colors.append("#10B981")     # beat or met
        else:
            bar_colors.append("#EF4444")     # miss
    ax1.plot(x, clean(d["prices"]), color=color, linewidth=2, zorder=3)
    ax1.scatter(x, clean(d["prices"]), c=bar_colors, s=70, zorder=5,
                edgecolors="white", linewidth=0.8)

    # Label each beat point with its EPS surprise % so a green dot reads
    # as "beat, and by how much" without cross-referencing Panel 2.
    prices_clean = clean(d["prices"])
    y_span = (np.nanmax(prices_clean) - np.nanmin(prices_clean)) if len(prices_clean) else 0
    y_offset = max(y_span * 0.06, 1.0)
    for xi, price, bcolor, surp in zip(x, prices_clean, bar_colors, surprise_pct):
        if bcolor == "#10B981" and surp is not None and not np.isnan(price):
            ax1.annotate(f"+{surp:.0f}%", (xi, price + y_offset),
                         ha="center", va="bottom", fontsize=fs(7),
                         fontweight="bold", color="#10B981")

    set_panel_title(ax1, "Price vs EPS Beat/Miss")
    ax1.set_xticks(x); ax1.set_xticklabels(q_labels, fontsize=fs(8))
    ax1.tick_params(axis="y", labelsize=fs(8))
    legend1 = [
        Line2D([0],[0], marker="o", color="w", markerfacecolor="#10B981",
               markersize=8, label="Beat / met"),
        Line2D([0],[0], marker="o", color="w", markerfacecolor="#EF4444",
               markersize=8, label="Miss"),
    ]
    ax1.legend(handles=legend1, fontsize=fs(7))
    beat_miss = []
    for ea, ee in zip(d["eps_actual"], d["eps_estimate"]):
        if ea is None or ee is None:
            beat_miss.append("—")
        else:
            beat_miss.append("Beat" if ea >= ee else "Miss")
    _draw_panel_table(tbl1, q_labels, [
        ("Price ($)", color,     d["prices"],          lambda v: f"${v:,.2f}"),
        ("React (%)", "#374151", d["price_react_pct"], lambda v: f"{v:+.1f}%"),
        ("Result",    "#374151", beat_miss,             lambda v: v),
    ])

    # ── Panel 2: EPS — Actual vs Estimate ────────────────────────────────────
    latest_surprise = latest(clean(surprise_pct))
    title2_stat = f"Latest surprise {latest_surprise:+.1f}%" if not np.isnan(latest_surprise) else None
    ax2, tbl2 = _make_panel_gridspec(fig, gs[0, 1])
    ax2.set_axisbelow(True)
    bw = 0.38
    ax2.bar(x - bw/2, clean(d["eps_actual"]),   width=bw, color=color,
            alpha=0.85, label="Actual")
    ax2.bar(x + bw/2, clean(d["eps_estimate"]), width=bw, color="#9CA3AF",
            alpha=0.70, label="Estimate")
    set_panel_title(ax2, "EPS — Actual vs Estimate ($)", title2_stat)
    ax2.set_xticks(x); ax2.set_xticklabels(q_labels, fontsize=fs(8))
    ax2.tick_params(axis="y", labelsize=fs(8))
    ax2.legend(fontsize=fs(7))
    add_zero_line(ax2)
    _draw_panel_table(tbl2, q_labels, [
        ("Actual ($)",   color,     d["eps_actual"],   lambda v: f"${v:.2f}"),
        ("Estimate ($)", "#6B7280", d["eps_estimate"], lambda v: f"${v:.2f}"),
        ("Surprise (%)", "#9333EA", surprise_pct,      lambda v: f"{v:+.1f}%"),
    ])

    # ── Panel 3: Revenue, Net Income & Net Margin ────────────────────────────
    rev_yoy = yoy_pct(d["revenue"])
    latest_rev_yoy = latest(clean(rev_yoy))
    title3_stat = f"Revenue YoY {latest_rev_yoy:+.1f}%" if not np.isnan(latest_rev_yoy) else None

    ax3, tbl3 = _make_panel_gridspec(fig, gs[0, 2])
    ax3.set_axisbelow(True)
    ax3b = ax3.twinx()
    ax3b.set_axisbelow(True)
    ax3b.grid(False)

    ax3.bar(x - bw/2, clean(d["revenue"]),    width=bw, color=color,
            alpha=0.55, label="Revenue ($)")
    ax3.bar(x + bw/2, clean(d["net_income"]), width=bw, color="#06B6D4",
            alpha=0.80, label="Net Income ($)")
    ax3b.plot(x, clean(d["net_margin"]), color="#F59E0B", linewidth=2,
              marker="o", markersize=4)

    set_panel_title(ax3, "Revenue, Net Income & Net Margin", title3_stat)
    ax3.set_xticks(x); ax3.set_xticklabels(q_labels, fontsize=fs(8))
    ax3.tick_params(axis="y", labelsize=fs(8))
    apply_money_axis(ax3)
    ax3b.tick_params(axis="y", labelsize=fs(8), colors="#F59E0B")
    ax3b.set_ylabel("Net Margin %", fontsize=fs(8), color="#F59E0B")
    legend3 = [
        Line2D([0],[0], color=color,     linewidth=6, alpha=0.75, label="Revenue ($)"),
        Line2D([0],[0], color="#06B6D4", linewidth=6, alpha=0.85, label="Net Income ($)"),
        Line2D([0],[0], color="#F59E0B", linewidth=2,              label="Net Margin (%)"),
    ]
    ax3.legend(handles=legend3, fontsize=fs(7))
    add_zero_line(ax3)
    _draw_panel_table(tbl3, q_labels, [
        ("Revenue ($)",    color,     d["revenue"],    lambda v: f"${v/1e6:,.0f}M"),
        ("Net Inc. ($)",   "#06B6D4", d["net_income"], lambda v: f"${v/1e6:,.0f}M"),
        ("Net Margin (%)", "#F59E0B", d["net_margin"], lambda v: f"{v:.1f}%"),
        ("Rev YoY (%)",    "#7C3AED", rev_yoy,          lambda v: f"{v:+.1f}%"),
    ])

    # ── Panel 4: CapEx Intensity ──────────────────────────────────────────────
    # CapitalExpenditure comes back from yfinance as a negative (cash
    # outflow) — abs() it so the bar height and the intensity ratio both
    # read the intuitive way ("capex is N% of OCF", not a negative %).
    ax4, tbl4 = _make_panel_gridspec(fig, gs[1, 0])
    ax4.set_axisbelow(True)
    ax4b = ax4.twinx()
    ax4b.set_axisbelow(True)
    ax4b.grid(False)

    capex_abs = [abs(v) if v is not None else None for v in d["capex"]]
    intensity = []
    for o, c in zip(d["ocf"], capex_abs):
        intensity.append(round(c / o * 100, 1) if (o and c is not None) else None)

    ax4.bar(x - bw/2, clean(d["ocf"]),    width=bw, color=color,
            alpha=0.55, label="OCF ($)")
    ax4.bar(x + bw/2, clean(capex_abs),   width=bw, color="#DC2626",
            alpha=0.75, label="CapEx ($)")
    ax4b.plot(x, clean(intensity), color="#7C3AED", linewidth=2,
              marker="s", markersize=4)

    latest_intensity = latest(clean(intensity))
    title4_stat = f"Latest CapEx/OCF {latest_intensity:.0f}%" if not np.isnan(latest_intensity) else None
    set_panel_title(ax4, "CapEx Intensity", title4_stat)
    ax4.set_xticks(x); ax4.set_xticklabels(q_labels, fontsize=fs(8))
    ax4.tick_params(axis="y", labelsize=fs(8))
    apply_money_axis(ax4)
    ax4b.tick_params(axis="y", labelsize=fs(8), colors="#7C3AED")
    ax4b.set_ylabel("CapEx / OCF %", fontsize=fs(8), color="#7C3AED")
    legend4 = [
        Line2D([0],[0], color=color,     linewidth=6, alpha=0.75, label="OCF ($)"),
        Line2D([0],[0], color="#DC2626", linewidth=6, alpha=0.85, label="CapEx ($)"),
        Line2D([0],[0], color="#7C3AED", linewidth=2,              label="CapEx/OCF (%)"),
    ]
    ax4.legend(handles=legend4, fontsize=fs(7))
    add_zero_line(ax4)
    _draw_panel_table(tbl4, q_labels, [
        ("OCF ($)",       color,     d["ocf"],   lambda v: f"${v/1e6:,.0f}M"),
        ("CapEx ($)",     "#DC2626", capex_abs,  lambda v: f"${v/1e6:,.0f}M"),
        ("CapEx/OCF (%)", "#7C3AED", intensity,  lambda v: f"{v:.0f}%"),
    ])

    # ── Panel 5: Debt Level & Coverage ───────────────────────────────────────
    fcf_margin_q = []
    for f, r in zip(d["fcf"], d["revenue"]):
        fcf_margin_q.append(round(f / r * 100, 1) if (r and f is not None) else None)
    latest_fcf_margin = latest(clean(fcf_margin_q))
    title5_stat = f"FCF Margin {latest_fcf_margin:.1f}%" if not np.isnan(latest_fcf_margin) else None

    ax5, tbl5 = _make_panel_gridspec(fig, gs[1, 1])
    ax5.set_axisbelow(True)
    tbw = 0.27
    ax5.bar(x - tbw, clean(d["debt"]), width=tbw, color="#F472B6", alpha=0.85, label="Debt ($)")
    ax5.bar(x,       clean(d["fcf"]),  width=tbw, color="#34D399", alpha=0.85, label="FCF ($)")
    ax5.bar(x + tbw, clean(d["cash"]), width=tbw, color=color,     alpha=0.55, label="Cash ($)")

    set_panel_title(ax5, "Debt Level & Coverage", title5_stat)
    ax5.set_xticks(x); ax5.set_xticklabels(q_labels, fontsize=fs(8))
    ax5.tick_params(axis="y", labelsize=fs(8))
    apply_money_axis(ax5)
    ax5.legend(fontsize=fs(7))
    add_zero_line(ax5)
    _draw_panel_table(tbl5, q_labels, [
        ("Debt ($)", "#F472B6", d["debt"], lambda v: f"${v/1e6:,.0f}M"),
        ("FCF ($)",  "#34D399", d["fcf"],  lambda v: f"${v/1e6:,.0f}M"),
        ("Cash ($)", color,     d["cash"], lambda v: f"${v/1e6:,.0f}M"),
        ("FCF Margin (%)", "#10B981", fcf_margin_q, lambda v: f"{v:.1f}%"),
    ])

    # ── Panel 6: P/E Trend, flagged for whether EPS actually held flat ──────
    # Raw quarterly P/E is only a clean sentiment signal when EPS itself
    # didn't move much that quarter — otherwise the ratio's confounded by
    # the earnings denominator shifting too. The marker shape flags which
    # case each point is, so a dip can be read as a real benchmark only
    # when it's the filled-circle kind.
    ax6, tbl6 = _make_panel_gridspec(fig, gs[1, 2])
    ax6.plot(x, clean(d["pe"]), color=color, linewidth=2, zorder=3)

    eps_qoq = [None]
    for i in range(1, len(d["eps_actual"])):
        prev, cur = d["eps_actual"][i - 1], d["eps_actual"][i]
        if prev and cur is not None:
            eps_qoq.append(round((cur - prev) / abs(prev) * 100, 1))
        else:
            eps_qoq.append(None)

    STABLE_THRESHOLD = 5.0  # EPS QoQ% within ±5% counts as "fundamentals held"
    for xi, pe_val, qoq in zip(x, d["pe"], eps_qoq):
        if pe_val is None:
            continue
        stable = qoq is not None and abs(qoq) <= STABLE_THRESHOLD
        ax6.scatter([xi], [pe_val], marker="o" if stable else "x",
                    s=70, zorder=5,
                    color="#10B981" if stable else "#9CA3AF")

    set_panel_title(ax6, "P/E Trend")
    ax6.set_xticks(x); ax6.set_xticklabels(q_labels, fontsize=fs(8))
    ax6.tick_params(axis="y", labelsize=fs(8))
    legend6 = [
        Line2D([0],[0], color=color, linewidth=2, label="P/E"),
        Line2D([0],[0], marker="o", color="w", markerfacecolor="#10B981",
               markersize=8, label="EPS held flat"),
        Line2D([0],[0], marker="x", color="#9CA3AF", markersize=8,
               linestyle="None", label="EPS also moved"),
    ]
    ax6.legend(handles=legend6, fontsize=fs(7))
    add_zero_line(ax6)
    _draw_panel_table(tbl6, q_labels, [
        ("P/E",         color,     d["pe"], lambda v: f"{v:.1f}x"),
        ("EPS QoQ (%)", "#374151", eps_qoq, lambda v: f"{v:+.1f}%"),
    ])

    # ── Footer ────────────────────────────────────────────────────────────────
    latest_pe   = latest(d["pe"])
    latest_nm   = latest(d["net_margin"])
    latest_reac = latest(d["price_react_pct"])

    def avg_last_n(values, n):
        vals = [v for v in values if v is not None and not (isinstance(v, float) and np.isnan(v))]
        subset = vals[-n:]
        return round(sum(subset) / len(subset), 1) if subset else None

    avg_pe_q = avg_last_n(d["pe"], 5)
    avg_nm_q = avg_last_n(d["net_margin"], 5)

    summary_parts = [f"{len(q_labels)} quarter(s) on file"]
    if not np.isnan(latest_pe):
        summary_parts.append(f"Latest P/E: {latest_pe:.1f}x")
    if avg_pe_q is not None:
        summary_parts.append(f"Avg P/E: {avg_pe_q:.1f}x")
    if not np.isnan(latest_nm):
        summary_parts.append(f"Latest Net Margin: {latest_nm:.1f}%")
    if avg_nm_q is not None:
        summary_parts.append(f"Avg Net Margin: {avg_nm_q:.1f}%")
    if not np.isnan(latest_reac):
        summary_parts.append(f"Latest Reaction: {latest_reac:+.1f}%")

    ax_footer = fig.add_subplot(gs[2, :])
    ax_footer.axis("off")
    ax_footer.text(0.5, 0.5, "  |  ".join(summary_parts),
             ha="center", va="center", transform=ax_footer.transAxes,
             fontsize=fs(9), fontweight="bold", color="#1A1A2E", fontfamily="monospace")

    return fig


# ─────────────────────────────────────────────────────────────────────────────
# 6. COMPARISON CHARTS
# ─────────────────────────────────────────────────────────────────────────────

def plot_comparison(data_list, colors):
    apply_style()
    fig, axes = plt.subplots(3, 3, figsize=(18, 14), facecolor="white", dpi=100, layout="constrained")
    fig.suptitle("All Tickers — Side-by-Side Comparison",
                 fontsize=fs(14), fontweight="bold")

    base_years = data_list[0]["years"]
    yrs = year_labels(base_years)
    x   = np.arange(len(yrs))

    panels = [
        (axes[0,0], "prices",      "Share Price ($)"),
        (axes[0,1], "eps",         "EPS ($)"),
        (axes[0,2], "pe",          "P/E Ratio"),
        (axes[1,0], "ocfps",       "OCF / Share ($)"),
        (axes[1,1], "fcfps",       "FCF / Share ($)"),
        (axes[1,2], "revps",       "Revenue / Share ($)"),
        (axes[2,0], "roe",         "ROE (%)"),
        (axes[2,1], "debt_assets", "Debt / Assets (%)"),
        (axes[2,2], "divps",       "Div / Share ($)"),
    ]

    for ax, field, title in panels:
        for d, col in zip(data_list, colors):
            vals = []
            for yr in base_years:
                if yr in d["years"]:
                    idx = d["years"].index(yr)
                    v = d[field][idx]
                    if field == "debt_assets" and v is not None:
                        v = v * 100
                    vals.append(v)
                else:
                    vals.append(None)
            ax.plot(yrs, clean(vals), color=col, linewidth=1.8,
                    marker="o", markersize=3, label=d["symbol"])
        ax.set_title(title, fontsize=fs(10), fontweight="bold")
        ax.set_xticks(x[::2]); ax.set_xticklabels(yrs[::2], fontsize=fs(8), rotation=30)
        ax.tick_params(axis="y", labelsize=fs(8))
        add_zero_line(ax)
        ticker_legend(ax, data_list, colors)

    # layout="constrained" (set at figure creation) handles spacing on
    # every draw, including window resize — no manual tight_layout needed.
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# 7. SNAPSHOT BAR CHART
# ─────────────────────────────────────────────────────────────────────────────

def plot_snapshot(data_list, colors):
    apply_style()

    metrics = [
        ("eps",         "EPS ($)",           "$"),
        ("pe",          "P/E Ratio",         "x"),
        ("roe",         "ROE (%)",           "%"),
        ("debt_assets", "Debt/Assets (%)",   "%"),
        ("ocfps",       "OCF/Share ($)",     "$"),
        ("fcfps",       "FCF/Share ($)",     "$"),
        ("revps",       "Revenue/Share ($)", "$"),
        ("divps",       "Div/Share ($)",     "$"),
    ]

    fig, axes = plt.subplots(2, 4, figsize=(18, 9), facecolor="white", dpi=100, layout="constrained")
    fig.suptitle("Latest Year — All Tickers Snapshot",
                 fontsize=fs(14), fontweight="bold")

    syms = [d["symbol"] for d in data_list]
    x    = np.arange(len(syms))

    for ax, (field, title, unit) in zip(axes.flat, metrics):
        vals = []
        for d in data_list:
            v = latest(d[field])
            if field == "debt_assets" and not np.isnan(v):
                v = v * 100
            vals.append(v)

        bar_colors = [c if not np.isnan(v) else "#ddd"
                      for c, v in zip(colors, vals)]
        vals_plot = [0 if np.isnan(v) else v for v in vals]
        ax.bar(x, vals_plot, color=bar_colors, alpha=1.0, width=0.6, linewidth=0, edgecolor="none")
        ax.set_xticks(x)
        ax.set_xticklabels(syms, fontsize=fs(11), fontweight="bold", rotation=45, ha="right")
        ax.set_title(title, fontsize=fs(10), fontweight="bold")
        ax.tick_params(axis="y", labelsize=fs(8))
        add_zero_line(ax)
        ax.grid(False)

        for xi, v in enumerate(vals_plot):
            if v == 0:
                continue
            label = f"{v:.1f}" if abs(v) < 1000 else f"{v/1000:.1f}k"
            ax.text(xi, v + (max(vals_plot) * 0.02 if v >= 0 else min(vals_plot) * 0.02),
                    label, ha="center", va="bottom" if v >= 0 else "top",
                    fontsize=fs(11), fontweight="bold", fontfamily="monospace")

    # layout="constrained" (set at figure creation) handles spacing on
    # every draw, including window resize — no manual tight_layout needed.
    return fig

def cagr(prices, n):
    clean_prices = [p for p in prices if p is not None]
    if len(clean_prices) < n + 1:
        return None
    end   = clean_prices[-1]
    start = clean_prices[-(n + 1)]
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return round(((end / start) ** (1 / n) - 1) * 100, 2)


def plot_etf(etf_list, colors, years_back):
    apply_style()
    n = len(etf_list)

    fig = plt.figure(figsize=(18, 10), facecolor="white", dpi=100, layout="constrained")
    fig.suptitle("ETF Overview — Price, Distributions & Annual Return",
                 fontsize=fs(14), fontweight="bold")

    gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.4, wspace=0.35)

    ax0 = fig.add_subplot(gs[0, 0])
    ax1 = fig.add_subplot(gs[0, 1])
    ax2 = fig.add_subplot(gs[1, 0])
    ax3 = fig.add_subplot(gs[1, 1])

    base_years = etf_list[0]["years"]
    yrs = year_labels(base_years)
    x   = np.arange(len(yrs))

    for d, col in zip(etf_list, colors):
        vals = []
        for yr in base_years:
            if yr in d["years"]:
                vals.append(d["prices"][d["years"].index(yr)])
            else:
                vals.append(None)
        ax0.plot(yrs, clean(vals), color=col, linewidth=2,
                 marker="o", markersize=4, label=d["symbol"])
    ax0.set_title("Price ($)", fontsize=fs(10), fontweight="bold")
    ax0.set_xticks(x[::2]); ax0.set_xticklabels(yrs[::2], fontsize=fs(8), rotation=30)
    ticker_legend(ax0, etf_list, colors)
    add_zero_line(ax0)

    for d, col in zip(etf_list, colors):
        vals = []
        for yr in base_years:
            if yr in d["years"]:
                vals.append(d["annual_returns"][d["years"].index(yr)])
            else:
                vals.append(None)
        ax1.plot(yrs, clean(vals), color=col, linewidth=2,
                 marker="o", markersize=4, label=d["symbol"])
    ax1.set_title("Annual Return (%)", fontsize=fs(10), fontweight="bold")
    ax1.set_xticks(x[::2]); ax1.set_xticklabels(yrs[::2], fontsize=fs(8), rotation=30)
    ticker_legend(ax1, etf_list, colors)
    add_zero_line(ax1)

    width = 0.8 / max(n, 1)
    for idx, (d, col) in enumerate(zip(etf_list, colors)):
        vals = []
        for yr in base_years:
            if yr in d["years"]:
                vals.append(d["distributions"][d["years"].index(yr)])
            else:
                vals.append(0)
        offset = (idx - n / 2 + 0.5) * width
        ax2.bar(x + offset, vals, width=width, color=col,
                alpha=0.85, label=d["symbol"], linewidth=0)
    ax2.set_title("Annual Distributions ($)", fontsize=fs(10), fontweight="bold")
    ax2.set_xticks(x[::2]); ax2.set_xticklabels(yrs[::2], fontsize=fs(8), rotation=30)
    ax2.grid(False)
    ticker_legend(ax2, etf_list, colors)
    add_zero_line(ax2)

    for d, col in zip(etf_list, colors):
        raw = []
        for yr in base_years:
            if yr in d["years"]:
                raw.append(d["prices"][d["years"].index(yr)])
            else:
                raw.append(None)

        base_price = next((v for v in raw if v is not None), None)
        if base_price is None:
            continue

        cumulative = [
            round((v / base_price) * 100, 2) if v is not None else None
            for v in raw
        ]
        ax3.plot(yrs, clean(cumulative), color=col, linewidth=2,
                 marker="o", markersize=4, label=d["symbol"])

    ax3.axhline(100, color="#ccc", linewidth=0.8, linestyle="--", zorder=0)
    ax3.set_title("Cumulative Total Return (Base = 100)", fontsize=fs(10), fontweight="bold")
    ax3.set_xticks(x[::2]); ax3.set_xticklabels(yrs[::2], fontsize=fs(8), rotation=30)
    ticker_legend(ax3, etf_list, colors)

    # layout="constrained" (set at figure creation) handles spacing on
    # every draw, including window resize — no manual tight_layout needed.
    return fig


def export_session(stock_list, stock_colors, etf_list, etf_colors,
                   figs_stock_single, fig_comparison, fig_snapshot,
                   fig_etf, fig_etf_table, fig_growth_table, fig_valuation_table,
                   years_back, quarterly_pairs=None, quarterly_table_data=None):
    today = datetime.now().strftime("%Y-%m-%d")
    out   = make_session_folder(stock_list, etf_list)
    saved = []

    if stock_list:
        # ── Combined scorecard CSV — every metric from both on-screen
        # tables (Growth & Quality + Valuation/Balance Sheet/Capital
        # Return) lives in one file, regardless of which window shows it.
        path = os.path.join(out, f"{today}_scorecard.csv")

        def avg_last_n(values, n):
            clean_vals = [v for v in values if v is not None]
            subset = clean_vals[-n:]
            return round(sum(subset) / len(subset), 2) if subset else None

        fieldnames = [
            "symbol", "name", "price", "price_cagr_full",
            # Growth
            "eps_cagr_full", "eps_cagr_3yr", "eps_cagr_5yr",
            "rev_cagr_3yr", "rev_cagr_5yr",
            "fcf_cagr_full",
            # Quality
            "gross_margin_latest", "gross_margin_trend",
            "op_margin_latest", "op_margin_trend",
            "net_margin_latest", "net_margin_trend",
            "fcf_margin_latest",
            # Valuation
            "trailing_pe", "forward_pe", "pe_5yr_avg", "fwd_vs_avg_pe",
            "price_fcf", "ev_ebitda",
            # Balance sheet
            "net_debt_ebitda", "interest_coverage",
            # Capital return
            "buyback_yield", "dividend_yield", "total_shareholder_yield",
            # Existing
            "roe_latest", "roe_3yr_avg",
            "fcfps_latest", "fcfps_3yr_avg",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for d in stock_list:
                avg_pe  = avg_last_n(d["pe"], 5)
                fwd_pe  = d.get("forward_pe")
                fwd_pe  = float(fwd_pe) if fwd_pe is not None else None
                cur_pe  = d.get("trailing_pe")
                cur_pe  = float(cur_pe) if cur_pe is not None else None
                fwd_avg = round(fwd_pe / avg_pe, 2) if fwd_pe and avg_pe and avg_pe > 0 else None

                roe_l = latest(d["roe"])
                fcf_l = latest(d["fcfps"])
                price_fcf = None
                cp = d.get("current_price")
                if cp is not None and not np.isnan(fcf_l) and fcf_l > 0:
                    price_fcf = round(float(cp) / fcf_l, 1)

                bb_yield  = d.get("buyback_yield")
                div_yield = d.get("dividend_yield")
                tsy = (round(bb_yield + div_yield, 1)
                       if bb_yield is not None and div_yield is not None else None)

                writer.writerow({
                    "symbol":  d["symbol"],
                    "name":    d["name"],
                    "price":   d.get("current_price"),
                    "price_cagr_full": cagr_full_window(d["prices"], d["years"]),
                    "eps_cagr_full": cagr_full_window(d["eps"], d["years"]),
                    "eps_cagr_3yr":  cagr_pct(d["eps"], 3),
                    "eps_cagr_5yr":  cagr_pct(d["eps"], 5),
                    "rev_cagr_3yr":  cagr_pct(d.get("revps", []), 3),
                    "rev_cagr_5yr":  cagr_pct(d.get("revps", []), 5),
                    "fcf_cagr_full": cagr_full_window(d.get("fcfps", []), d["years"]),
                    "gross_margin_latest": (lambda v: None if np.isnan(v) else v)(latest(d.get("gross_margin", []))),
                    "gross_margin_trend":  margin_trend(d.get("gross_margin", [])),
                    "op_margin_latest":    (lambda v: None if np.isnan(v) else v)(latest(d.get("op_margin", []))),
                    "op_margin_trend":     margin_trend(d.get("op_margin", [])),
                    "net_margin_latest":   (lambda v: None if np.isnan(v) else v)(latest(d.get("net_margin", []))),
                    "net_margin_trend":    margin_trend(d.get("net_margin", [])),
                    "fcf_margin_latest":   (lambda v: None if np.isnan(v) else v)(latest(d.get("fcf_margin", []))),
                    "trailing_pe":     cur_pe,
                    "forward_pe":      fwd_pe,
                    "pe_5yr_avg":      avg_pe,
                    "fwd_vs_avg_pe":   fwd_avg,
                    "price_fcf":       price_fcf,
                    "ev_ebitda":       d.get("ev_ebitda"),
                    "net_debt_ebitda": d.get("net_debt_ebitda"),
                    "interest_coverage": d.get("interest_coverage"),
                    "buyback_yield":   bb_yield,
                    "dividend_yield":  div_yield,
                    "total_shareholder_yield": tsy,
                    "roe_latest":      None if np.isnan(roe_l) else roe_l,
                    "roe_3yr_avg":     avg_last_n(d["roe"], 3),
                    "fcfps_latest":    None if np.isnan(fcf_l) else fcf_l,
                    "fcfps_3yr_avg":   avg_last_n(d["fcfps"], 3),
                })
        saved.append(f"{today}_scorecard.csv")

    if quarterly_table_data:
        # ── Quarterly scorecard CSV — every metric from both quarterly
        # on-screen tables (Earnings Quality + Cash/Capital Intensity/
        # Valuation), one file, mirroring how the yearly scorecard.csv
        # combines its two tables above.
        path = os.path.join(out, f"{today}_quarterly_scorecard.csv")

        q_fieldnames = [
            "symbol", "name", "latest_quarter",
            "eps_surprise_pct", "beats", "beats_out_of",
            "revenue_yoy_pct",
            "net_margin_latest", "net_margin_5q_avg",
            "fcf_margin_latest", "capex_ocf_pct_latest",
            "pe_latest", "pe_5q_avg",
            "price_reaction_pct_latest",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=q_fieldnames)
            writer.writeheader()
            for d in quarterly_table_data:
                eps_actual   = d.get("eps_actual", [])
                eps_estimate = d.get("eps_estimate", [])
                revenue      = d.get("revenue", [])
                fcf          = d.get("fcf", [])
                ocf          = d.get("ocf", [])
                capex        = d.get("capex", [])
                net_margin   = d.get("net_margin", [])
                pe           = d.get("pe", [])
                reaction     = d.get("price_react_pct", [])

                surprise_series = []
                for ea, ee in zip(eps_actual, eps_estimate):
                    if ea is None or ee is None or ee == 0:
                        surprise_series.append(None)
                    else:
                        surprise_series.append(round((ea - ee) / abs(ee) * 100.0, 2))
                lat_surprise = latest(clean(surprise_series))

                compared = [(ea, ee) for ea, ee in zip(eps_actual, eps_estimate)
                            if ea is not None and ee is not None]
                beats      = sum(1 for ea, ee in compared if ea >= ee) if compared else None
                beats_of_n = len(compared) if compared else None

                yoy_series = [None] * len(revenue)
                for i in range(4, len(revenue)):
                    cur, prior = revenue[i], revenue[i - 4]
                    if cur is not None and prior is not None and prior != 0:
                        yoy_series[i] = round((cur - prior) / abs(prior) * 100.0, 2)
                lat_yoy = latest(clean(yoy_series))

                fcf_margin_series = []
                for fv, rv in zip(fcf, revenue):
                    fcf_margin_series.append(round(fv / rv * 100.0, 2) if (rv and fv is not None) else None)
                lat_fcf_margin = latest(clean(fcf_margin_series))

                intensity_series = []
                for o, c in zip(ocf, capex):
                    c_abs = abs(c) if c is not None else None
                    intensity_series.append(round(c_abs / o * 100.0, 1) if (o and c_abs is not None) else None)
                lat_intensity = latest(clean(intensity_series))

                q_ends = d.get("quarter_ends", [])
                fq     = d.get("fiscal_quarters", [])
                fy     = d.get("fiscal_years", [])
                latest_q_label = (f"Q{fq[-1]} '{str(fy[-1])[-2:]}"
                                  if q_ends and fq and fy else None)

                lat_nm  = latest(clean(net_margin))
                avg_nm  = avg_last_n(net_margin, 5)
                lat_pe  = latest(clean(pe))
                avg_pe  = avg_last_n(pe, 5)
                lat_rxn = latest(clean(reaction))

                writer.writerow({
                    "symbol": d.get("symbol", ""),
                    "name":   d.get("name", ""),
                    "latest_quarter": latest_q_label,
                    "eps_surprise_pct": None if np.isnan(lat_surprise) else lat_surprise,
                    "beats": beats,
                    "beats_out_of": beats_of_n,
                    "revenue_yoy_pct": None if np.isnan(lat_yoy) else lat_yoy,
                    "net_margin_latest": None if np.isnan(lat_nm) else lat_nm,
                    "net_margin_5q_avg": avg_nm,
                    "fcf_margin_latest": None if np.isnan(lat_fcf_margin) else lat_fcf_margin,
                    "capex_ocf_pct_latest": None if np.isnan(lat_intensity) else lat_intensity,
                    "pe_latest": None if np.isnan(lat_pe) else lat_pe,
                    "pe_5q_avg": avg_pe,
                    "price_reaction_pct_latest": None if np.isnan(lat_rxn) else lat_rxn,
                })
        saved.append(f"{today}_quarterly_scorecard.csv")

    if stock_list:
        path = os.path.join(out, f"{today}_ticker_detail.csv")

        fieldnames = [
            "symbol", "name", "fiscal_year",
            "price", "eps", "pe", "historical_peg",
            "roe", "bvps", "debt_assets_pct",
            "ocfps", "fcfps", "revps", "divps",
            "gross_margin", "op_margin", "net_margin", "fcf_margin", "shares_out",
            "current_price", "trailing_pe", "forward_pe", "peg_ratio",
            "analyst_tp", "analyst_low", "analyst_high", "consensus",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for d in stock_list:
                hist_peg = compute_historical_peg(d["pe"], d["eps"])
                gm = d.get("gross_margin", [None]*len(d["years"]))
                om = d.get("op_margin",    [None]*len(d["years"]))
                nm = d.get("net_margin",   [None]*len(d["years"]))
                fm = d.get("fcf_margin",   [None]*len(d["years"]))
                so = d.get("shares_out",   [None]*len(d["years"]))

                for i, yr in enumerate(d["years"]):
                    da = d["debt_assets"][i]
                    da_pct = round(da * 100, 2) if da is not None else None

                    writer.writerow({
                        "symbol": d["symbol"],
                        "name": d["name"],
                        "fiscal_year": yr,
                        "price": d["prices"][i],
                        "eps": d["eps"][i],
                        "pe": d["pe"][i],
                        "historical_peg": hist_peg[i],
                        "roe": d["roe"][i],
                        "bvps": d["bvps"][i],
                        "debt_assets_pct": da_pct,
                        "ocfps": d["ocfps"][i],
                        "fcfps": d["fcfps"][i],
                        "revps": d["revps"][i],
                        "divps": d["divps"][i],
                        "gross_margin": gm[i] if i < len(gm) else None,
                        "op_margin":    om[i] if i < len(om) else None,
                        "net_margin":   nm[i] if i < len(nm) else None,
                        "fcf_margin":   fm[i] if i < len(fm) else None,
                        "shares_out":   so[i] if i < len(so) else None,
                        "current_price": d.get("current_price"),
                        "trailing_pe": d.get("trailing_pe"),
                        "forward_pe": d.get("forward_pe"),
                        "peg_ratio": d.get("peg_ratio"),
                        "analyst_tp": d.get("analyst_tp"),
                        "analyst_low": d.get("analyst_low"),
                        "analyst_high": d.get("analyst_high"),
                        "consensus": d.get("consensus"),
                    })

        saved.append(f"{today}_ticker_detail.csv")

    if etf_list:
        path = os.path.join(out, f"{today}_etf_summary.csv")
        periods = sorted(set([1, 3, 5, 10, years_back - 1]))

        fieldnames = (
            ["symbol", "name", "current_price"]
            + [f"cagr_{p}yr" for p in periods]
            + ["best_year", "best_return", "worst_year", "worst_return",
               "avg_return", "volatility", "total_return", "yield_pct"]
        )

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for d in etf_list:
                valid_returns = [(yr, r) for yr, r in zip(d["years"], d["annual_returns"])
                                 if r is not None]
                clean_prices  = [p for p in d["prices"] if p is not None]

                best_yr = best_val = worst_yr = worst_val = avg = vol = total = yield_pct = None
                if valid_returns:
                    best_yr,  best_val  = max(valid_returns, key=lambda x: x[1])
                    worst_yr, worst_val = min(valid_returns, key=lambda x: x[1])
                    avg = round(sum(r for _, r in valid_returns) / len(valid_returns), 1)
                if len(valid_returns) >= 2:
                    vol = round(float(np.std([r for _, r in valid_returns], ddof=1)), 1)
                if len(clean_prices) >= 2:
                    total = round((clean_prices[-1] / clean_prices[0] - 1) * 100, 1)
                try:
                    latest_dist = next(
                        (d["distributions"][i] for i in range(len(d["years"]) - 1, -1, -1)
                         if d["distributions"][i] and d["distributions"][i] > 0), None)
                    cur = d.get("current_price")
                    if latest_dist and cur and cur > 0:
                        yield_pct = round(latest_dist / cur * 100, 2)
                except Exception:
                    pass

                row = {
                    "symbol":        d["symbol"],
                    "name":          d["name"],
                    "current_price": d.get("current_price"),
                    "best_year":     best_yr,
                    "best_return":   best_val,
                    "worst_year":    worst_yr,
                    "worst_return":  worst_val,
                    "avg_return":    avg,
                    "volatility":    vol,
                    "total_return":  total,
                    "yield_pct":     yield_pct,
                }
                for p in periods:
                    row[f"cagr_{p}yr"] = cagr(d["prices"], p)
                writer.writerow(row)
        saved.append(f"{today}_etf_summary.csv")

    for d, fig in zip(stock_list, figs_stock_single):
        fname = f"{today}_{d['symbol']}.png"
        fig.savefig(os.path.join(out, fname), dpi=150, bbox_inches="tight")
        saved.append(fname)

    for symbol, fig in (quarterly_pairs or []):
        fname = f"{today}_{symbol}_quarterly.png"
        fig.savefig(os.path.join(out, fname), dpi=150, bbox_inches="tight")
        saved.append(fname)

    pairs = [
        (fig_growth_table,    "scorecard_growth"),
        (fig_valuation_table, "scorecard_valuation"),
        (fig_comparison,      "comparison"),
        (fig_snapshot,        "snapshot"),
        (fig_etf,              "etf_overview"),
        (fig_etf_table,        "etf_table"),
    ]
    for fig, label in pairs:
        if fig is not None:
            fname = f"{today}_{label}.png"
            fig.savefig(os.path.join(out, fname), dpi=150, bbox_inches="tight")
            saved.append(fname)

    return saved, out

def make_session_folder(stock_list, etf_list):
    all_syms = [d["symbol"] for d in stock_list] + [d["symbol"] for d in etf_list]
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")

    if len(all_syms) <= 5:
        ticker_part = "_".join(all_syms)
    else:
        ticker_part = "_".join(all_syms[:3]) + f"_and_{len(all_syms) - 3}_more"

    folder_name = f"{timestamp}_{ticker_part}"
    folder_path = os.path.join(DB_OUTPUT, folder_name)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path


# ─────────────────────────────────────────────────────────────────────────────
# 8. MAIN
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# 7b. CHART PREFERENCES DIALOG
# ─────────────────────────────────────────────────────────────────────────────

PANEL_LABELS = {
    "panel2_pe":      ("Chart 2 — P/E & PEG",         "P/E"),
    "panel2_peg":     (None,                            "PEG"),
    "panel3_eps":     ("Chart 3 — EPS & ROE",          "EPS"),
    "panel3_roe":     (None,                            "ROE"),
    "panel4_bvps":    ("Chart 4 — BV/Share & Debt/Assets", "BV/Share"),
    "panel4_debt":    (None,                            "Debt/Assets"),
    "panel5_ocf":     ("Chart 5 — OCF/Share & FCF/Share",  "OCF/Share"),
    "panel5_fcf":     (None,                            "FCF/Share"),
    "panel6_rev":     ("Chart 6 — Revenue/Share & Div/Share", "Rev/Share"),
    "panel6_div":     (None,                            "Div/Share"),
}

def open_chart_prefs_dialog(parent):
    """Open a Toplevel window for editing per-series chart style preferences."""
    import tkinter as tk

    prefs = load_chart_prefs()

    dlg = tk.Toplevel(parent)
    dlg.title("Chart Style Preferences")
    dlg.configure(bg="#F7F9FC")
    dlg.resizable(False, False)
    dlg.grab_set()  # modal

    FONT_HDR   = ("Segoe UI", 14, "bold")
    FONT_PANEL = ("Segoe UI", 11, "bold")
    FONT_ITEM  = ("Segoe UI", 11)
    FONT_BTN   = ("Segoe UI", 11, "bold")
    FONT_NOTE  = ("Segoe UI", 10)
    BG         = "#F7F9FC"
    ACCENT     = "#00A4EF"

    tk.Label(
        dlg, text="Chart Style Preferences",
        bg=ACCENT, fg="white",
        font=FONT_HDR,
        padx=24, pady=14,
    ).pack(fill="x")

    tk.Label(
        dlg,
        text="  Chart 1 (Share Price) is always a line.",
        bg=BG, fg="#555577",
        font=FONT_NOTE,
        anchor="w",
    ).pack(fill="x", padx=24, pady=(10, 2))

    frame = tk.Frame(dlg, bg=BG, padx=24, pady=8)
    frame.pack(fill="x")

    tk.Label(frame, text="Series", bg=BG, fg="#888",
             font=("Segoe UI", 10), anchor="w").grid(
        row=0, column=0, sticky="w", padx=(0, 16))
    for ci, lbl in enumerate(("Bar", "Line"), start=1):
        tk.Label(frame, text=lbl, bg=BG, fg="#888",
                 font=("Segoe UI", 10)).grid(row=0, column=ci, padx=10)

    vars_ = {}
    grid_row = 1

    for key, (panel_title, series_label) in PANEL_LABELS.items():
        if panel_title is not None:
            tk.Label(
                frame, text=panel_title,
                bg=BG, fg="#1A1A2E",
                font=FONT_PANEL,
                anchor="w",
            ).grid(row=grid_row, column=0, columnspan=3, sticky="w",
                   pady=(12, 2))
            grid_row += 1

        tk.Label(
            frame, text=f"  {series_label}",
            bg=BG, fg="#1A1A2E",
            font=FONT_ITEM,
            anchor="w",
        ).grid(row=grid_row, column=0, sticky="w", pady=3, padx=(0, 16))

        var = tk.StringVar(value=prefs.get(key, "bar"))
        vars_[key] = var

        for ci, style in enumerate(("bar", "line"), start=1):
            tk.Radiobutton(
                frame,
                variable=var,
                value=style,
                bg=BG,
                selectcolor="#D0EEFF",
                activebackground=BG,
            ).grid(row=grid_row, column=ci, padx=10)

        grid_row += 1

    tk.Frame(dlg, bg="#CCCCCC", height=1).pack(fill="x", padx=24, pady=(8, 0))

    btn_row = tk.Frame(dlg, bg=BG, padx=24, pady=14)
    btn_row.pack(fill="x")

    def _save():
        updated = {k: v.get() for k, v in vars_.items()}
        updated["panel1_price"] = "line"
        save_chart_prefs(updated)
        dlg.destroy()

    def _cancel():
        dlg.destroy()

    tk.Button(
        btn_row, text="✓  Save",
        bg=ACCENT, fg="white",
        font=FONT_BTN,
        relief="flat", padx=20, pady=8, cursor="hand2",
        command=_save,
    ).pack(side="right")

    tk.Button(
        btn_row, text="Cancel",
        bg="#E5E7EB", fg="#1A1A2E",
        font=FONT_BTN,
        relief="flat", padx=16, pady=8, cursor="hand2",
        command=_cancel,
    ).pack(side="right", padx=(0, 10))

    dlg.update_idletasks()
    px = parent.winfo_x() + (parent.winfo_width()  - dlg.winfo_width())  // 2
    py = parent.winfo_y() + (parent.winfo_height() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{px}+{py}")
    dlg.wait_window()


def main():
    print(f"\n{'='*60}")
    print(f"  Fundamental Dashboard — starting up")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # Pick tickers — creates the Tk window, returns after Go is pressed
    _run_state = {
        "selected":      [],
        "years_back":    11,
        "force_refresh": False,
        "do_export":     False,
        "show_charts":   True,
        "show_quarterly": False,
        "quarterly_mode": "last_n",
        "quarters_back":  8,
        "quarter_start":  None,
        "quarter_end":    None,
    }

    selected, YEARS_BACK, force_refresh, do_export, \
        _root, _log, _title_var, _run_again_btn, _status_bottom, _exit_btn, _user_exited = pick_tickers(
            DB_PATH, _run_state, prefs_callback=open_chart_prefs_dialog
        )
    do_show = _run_state.get("show_charts", True)

    if not selected:
        print("No tickers selected. Exiting.")
        sys.exit(0)

    while True:
        # If the window was destroyed (user clicked Exit), stop cleanly.
        if _root is not None:
            try:
                if not _root.winfo_exists():
                    return
            except Exception:
                return

        # Re-bind log to the current window widgets each iteration
        _cur_log, _cur_title = _log, _title_var

        def log(msg, title=None, _l=_cur_log, _t=_cur_title):
            print(msg)
            post_status(_l, _t, msg, title=title)

        conn = get_db()
        print("Database contents:")
        print_db_summary(conn)
        print()

        # Re-read each iteration (not just once before the loop) for the same
        # reason do_show already is below — _run_state can change between
        # "Run Again" cycles on the same window.
        show_quarterly = _run_state.get("show_quarterly", False)
        quarterly_mode = _run_state.get("quarterly_mode", "last_n")
        quarters_back  = _run_state.get("quarters_back", 8)
        quarter_start  = _run_state.get("quarter_start")
        quarter_end    = _run_state.get("quarter_end")
        quarterly_map  = {}   # symbol -> quarterly dict, filled in below

        def fetch_quarterly(sym):
            """
            Stock-only — never call this for an ETF symbol. Refreshes this
            symbol's quarterly history if it's due (or force_refresh is on),
            then loads whatever's accumulated in the DB (filtered by the
            chosen display mode) into quarterly_map, keyed by symbol so the
            chart-building step below can look it up against stock_list
            without needing index-aligned parallel lists.
            """
            if not show_quarterly:
                return
            try:
                if force_refresh or is_quarter_stale(conn, sym):
                    log(f"  \u2193 {sym}  downloading (quarterly)\u2026")
                    qd = download_ticker_quarterly(sym)
                    if qd:
                        upsert_quarterly(conn, qd)
                        log(f"  \u2714 {sym}  quarterly saved")
                    else:
                        log(f"  \u2716 {sym}  quarterly download failed")
                q_loaded = load_quarterly_from_db(conn, sym, start=quarter_start, end=quarter_end)
                if q_loaded:
                    if quarterly_mode == "last_n":
                        q_loaded = trim_to_quarters(q_loaded, quarters_back)
                    quarterly_map[sym] = q_loaded
            except Exception as e:
                log(f"  \u2716 {sym}  quarterly error: {e}")

        stock_list, stock_colors = [], []
        etf_list,   etf_colors   = [], []

        log(f"Processing {len(selected)} ticker(s)\u2026", title="Downloading data\u2026")

        for i, sym in enumerate(selected):
            try:
                col = get_color(i)

                cached_etf = load_etf_from_db(conn, sym)
                if cached_etf and not force_refresh and not is_stale(conn, sym, YEARS_BACK):
                    log(f"  \u2714 {sym}  loaded from DB (ETF)")
                    etf_list.append(trim_to_years(cached_etf, YEARS_BACK))
                    etf_colors.append(col)
                    continue

                cached = load_ticker_from_db(conn, sym)
                if cached and not force_refresh and not is_stale(conn, sym, YEARS_BACK):
                    log(f"  \u2714 {sym}  loaded from DB")
                    stock_list.append(trim_to_years(cached, YEARS_BACK))
                    stock_colors.append(col)
                    fetch_quarterly(sym)
                    continue

                log(f"  \u2193 {sym}  downloading\u2026")
                t          = yf.Ticker(sym)
                quote_type = t.info.get("quoteType", "EQUITY")

                if quote_type == "ETF":
                    d = download_etf(sym, YEARS_BACK)
                    if d:
                        upsert_etf(conn, d, YEARS_BACK)
                        merged = load_etf_from_db(conn, sym)
                        etf_list.append(trim_to_years(merged if merged else d, YEARS_BACK))
                        etf_colors.append(col)
                        log(f"  \u2714 {sym}  saved (ETF)")
                    else:
                        log(f"  \u2716 {sym}  download failed")
                else:
                    d = download_ticker(sym, YEARS_BACK)
                    if d:
                        upsert_ticker(conn, d, YEARS_BACK)
                        merged = load_ticker_from_db(conn, sym)
                        stock_list.append(trim_to_years(merged if merged else d, YEARS_BACK))
                        stock_colors.append(col)
                        fetch_quarterly(sym)
                        log(f"  \u2714 {sym}  saved")
                    else:
                        log(f"  \u2716 {sym}  download failed")

            except Exception as e:
                import traceback
                log(f"  \u2716 {sym}  error: {e}")
                traceback.print_exc()

        log("\nExporting debug files\u2026", title="Saving files\u2026")
        export_summary_txt(conn)
        export_full_csv(conn)
        export_db_health(conn)
        log("  \u2714 db_summary.txt  db_full.csv  db_health.txt")

        conn.close()

        if not stock_list and not etf_list:
            log("\nNo data to display.")
            sys.exit(1)

        all_loaded = [d['symbol'] for d in stock_list] + [d['symbol'] for d in etf_list]
        log(f"\nLoaded: {', '.join(all_loaded)}", title="Building charts\u2026")

        do_show       = _run_state.get("show_charts", True)
        need_charts   = do_show or do_export

        apply_style()
        chart_prefs        = load_chart_prefs()
        figs_stock_single   = []
        figs_quarterly       = []
        quarterly_export_pairs = []   # (symbol, fig) — export_session needs the symbol too
        quarterly_table_data   = []   # per-symbol quarterly dicts, for the two quarterly scorecards
        fig_growth_table    = None   # interactive Tk window, not a matplotlib fig — stays None
        fig_valuation_table = None   # same
        fig_comparison      = None
        fig_snapshot        = None
        fig_etf             = None
        fig_etf_table       = None   # same — ETF table is also a Tk window

        if need_charts:
            for d, col in zip(stock_list, stock_colors):
                log(f"  Chart: {d['symbol']}")
                fig = plot_single_ticker(d, col, prefs=chart_prefs)
                fig.canvas.manager.set_window_title(f"{d['symbol']} \u2014 {d['name']}")
                figs_stock_single.append(fig)

                if show_quarterly:
                    q_data = quarterly_map.get(d["symbol"])
                    if q_data:
                        q_data["name"] = d.get("name", d["symbol"])
                        log(f"  Chart: {d['symbol']} (Quarterly Pulse)")
                        fig_q = plot_quarterly_pulse(q_data, col)
                        fig_q.canvas.manager.set_window_title(f"{d['symbol']} \u2014 Quarterly Pulse")
                        figs_quarterly.append(fig_q)
                        quarterly_export_pairs.append((d["symbol"], fig_q))
                        quarterly_table_data.append(q_data)
                    else:
                        log(f"  \u2014 {d['symbol']}  no quarterly data to chart")

            if stock_list:
                log("  Table: Scorecard — Growth & Quality")
                show_stock_table_growth(stock_list, stock_colors, YEARS_BACK)
                log("  Table: Scorecard — Valuation, Balance Sheet & Capital Return")
                show_stock_table_valuation(stock_list, stock_colors, YEARS_BACK)

            if show_quarterly and quarterly_table_data:
                log("  Table: Quarterly Scorecard — Earnings Quality")
                show_stock_table_quarterly_earnings(quarterly_table_data, stock_colors)
                log("  Table: Quarterly Scorecard — Cash, Capital Intensity & Valuation")
                show_stock_table_quarterly_valuation(quarterly_table_data, stock_colors)

            if len(stock_list) > 1:
                log("  Chart: Comparison")
                fig_comparison = plot_comparison(stock_list, stock_colors)
                fig_comparison.canvas.manager.set_window_title("Comparison \u2014 All Tickers")
                log("  Chart: Snapshot")
                fig_snapshot = plot_snapshot(stock_list, stock_colors)
                fig_snapshot.canvas.manager.set_window_title("Snapshot \u2014 Latest Year")

            if etf_list:
                log("  Chart: ETF Overview")
                fig_etf = plot_etf(etf_list, etf_colors, YEARS_BACK)
                fig_etf.canvas.manager.set_window_title("ETF Overview")
                log("  Table: ETF Scorecard")
                show_etf_table(etf_list, etf_colors, YEARS_BACK)

        figs = figs_stock_single[:] + figs_quarterly
        for f in [fig_growth_table, fig_valuation_table, fig_comparison,
                  fig_snapshot, fig_etf, fig_etf_table]:
            if f is not None:
                figs.append(f)

        if do_export:
            log("\nExporting session files\u2026", title="Exporting\u2026")
            saved, session_folder = export_session(
                stock_list, stock_colors, etf_list, etf_colors,
                figs_stock_single, fig_comparison, fig_snapshot,
                fig_etf, fig_etf_table, fig_growth_table, fig_valuation_table,
                YEARS_BACK, quarterly_export_pairs,
                quarterly_table_data=quarterly_table_data,
            )
            log(f"  \u2714 {len(saved)} files \u2192 {session_folder}")

        log("\n\u2714 All done \u2014 close charts when finished.", title="Charts ready")
        try:
            if _run_again_btn is not None and _run_again_btn.winfo_exists():
                _run_again_btn.pack(side="left")
            if _exit_btn is not None and _exit_btn.winfo_exists():
                _exit_btn.pack(side="right")
        except Exception:
            pass

        if do_show and need_charts:
            if _root is not None:
                try:
                    if not _root.winfo_exists():
                        return
                except Exception:
                    return
            plt.show()

        try:
            if _root is not None and _root.winfo_exists():
                _title_var.set("Done \u2014 run again?")
                _root.mainloop()
        except Exception:
            pass

        if not _run_state["selected"]:
            print("No tickers selected. Exiting.")
            sys.exit(0)
        selected      = _run_state["selected"]
        YEARS_BACK    = _run_state["years_back"]
        force_refresh = _run_state["force_refresh"]
        do_export     = _run_state["do_export"]
        do_show       = _run_state.get("show_charts", True)


def is_stale(conn, symbol, years_back, days=90):
    row = conn.execute(
        "SELECT last_updated, consensus, years_stored, history_exhausted FROM tickers WHERE symbol = ?",
        (symbol,)
    ).fetchone()
    if not row:
        return True

    is_etf = (row["consensus"] == "ETF")
    table  = "etf_data" if is_etf else "annual_data"
    count  = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE symbol = ?", (symbol,)
    ).fetchone()[0]
    if count == 0:
        return True

    years_stored      = row["years_stored"] or 0
    history_exhausted = bool(row["history_exhausted"])
    if years_back > years_stored and not history_exhausted:
        return True

    parsed = dateutil.parser.parse(row["last_updated"])
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    age = datetime.now() - parsed
    return age.total_seconds() > days * 86400


def is_quarter_stale(conn, symbol, days=21):
    """
    Quarterly equivalent of is_stale(). Deliberately checks far more often
    than the annual 90-day window — a new quarter is reported roughly every
    ~90 days, so reusing that same window would mean we'd often sit on a
    stale local copy for weeks after a fresh print actually landed. A
    shorter window catches it within a few runs instead.

    This only governs whether we bother re-fetching. Once we do,
    upsert_quarterly() never deletes existing rows — it only adds rows for
    quarter_end dates not already present. So an over-eager stale check
    just costs an extra yfinance call; an under-eager one just means
    catching a new quarter a little later. Neither ever loses history.
    """
    row = conn.execute(
        "SELECT consensus FROM tickers WHERE symbol = ?", (symbol,)
    ).fetchone()
    if not row:
        return True
    if row["consensus"] == "ETF":
        return False  # quarterly income statements aren't meaningful for ETFs

    latest = conn.execute(
        "SELECT quarter_end, last_updated FROM quarterly_data "
        "WHERE symbol = ? ORDER BY quarter_end DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    if not latest:
        return True

    parsed = dateutil.parser.parse(latest["last_updated"])
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    if (datetime.now() - parsed).total_seconds() > days * 86400:
        return True

    # Belt-and-suspenders: if the newest quarter on file is itself already
    # ~100+ days old, a new one has almost certainly been reported by now
    # regardless of when we last checked — don't wait for the timer above.
    q_end = dateutil.parser.parse(latest["quarter_end"])
    return (datetime.now() - q_end).days > 100


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("\n" + "="*60)
        print("  UNHANDLED ERROR")
        print("="*60)
        traceback.print_exc()
        input("\nPress Enter to close...")