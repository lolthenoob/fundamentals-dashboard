"""
IT Sector Fundamentals Dashboard
=================================
Auto-downloads data from Yahoo Finance via yfinance.
Saves/updates all ticker data to tickers/fundamentals.db (SQLite).

CONFIGURE YOUR TICKERS HERE:
"""

'TICKERS = ["MSFT", "AAPL", "NVDA", "AVGO", "ORCL", "AMD", "QCOM", "TXN", "ACN", "IBM"]'
'TICKERS = ["MSFT"]'

# How many years of annual history to show
'YEARS_BACK = 11'

# ─────────────────────────────────────────────────────────────────────────────
from ticker_picker import pick_tickers, post_status
from interactive_table import show_stock_table, show_etf_table
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
from matplotlib.lines import Line2D
import yfinance as yf
from datetime import datetime
import mplcursors


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

def upsert_ticker(conn, d, years_requested):
    now = datetime.now().isoformat(timespec="seconds")
    years_stored      = len(d["years"])
    history_exhausted = 1 if years_stored < years_requested else 0

    conn.execute("""
        INSERT INTO tickers
            (symbol, name, current_price, analyst_tp, analyst_low, analyst_high,
             consensus, trailing_pe, forward_pe, peg_ratio, last_updated,
             years_stored, history_exhausted)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            history_exhausted = excluded.history_exhausted
    """, (
        d["symbol"], d["name"], d["current_price"],
        d["analyst_tp"], d["analyst_low"], d["analyst_high"],
        d["consensus"], d["trailing_pe"], d["forward_pe"],
        d.get("peg_ratio"), now,
        years_stored, history_exhausted,
    ))

    rows = zip(
        d["years"],  d["prices"], d["eps"],   d["pe"],
        d["roe"],    d["bvps"],   d["debt_assets"],
        d["ocfps"],  d["fcfps"],  d["revps"],  d["divps"],
    )
    conn.executemany("""
        INSERT INTO annual_data
            (symbol, fiscal_year, price, eps, pe, roe, bvps,
             debt_assets, ocfps, fcfps, revps, divps)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
            divps       = excluded.divps
    """, [(d["symbol"], yr, p, e, pe, roe, bvps, da, ocf, fcf, rev, div)
          for yr, p, e, pe, roe, bvps, da, ocf, fcf, rev, div in rows])

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
        "current_price": row["current_price"],
        "analyst_tp":    row["analyst_tp"],
        "analyst_low":   row["analyst_low"],
        "analyst_high":  row["analyst_high"],
        "consensus":     row["consensus"],
        "trailing_pe":   row["trailing_pe"],
        "forward_pe":    row["forward_pe"],
        "peg_ratio":     row["peg_ratio"],
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
            a.ocfps, a.fcfps, a.revps, a.divps
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
            "current_price": current_price,
            "analyst_tp":    analyst_tp,
            "analyst_low":   analyst_low,
            "analyst_high":  analyst_high,
            "consensus":     consensus,
            "trailing_pe":   trailing_pe,
            "forward_pe":    forward_pe,
            "peg_ratio":     peg_ratio,
            "short_float":   short_float,
        }

    except Exception as e:
        print(f"FAILED ({e})")
        return None

def download_etf(symbol, years_back):
    print(f"  Downloading {symbol} (ETF) ...", end=" ", flush=True)
    try:
        t    = yf.Ticker(symbol)
        info = t.info
        hist = t.history(period="max", interval="1mo")
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
]
ETF_LIST_FIELDS = ["years", "prices", "distributions", "annual_returns"]


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

def apply_style():
    plt.rcParams.update(STYLE)

def ticker_legend(ax, data_list, colors):
    handles = [Line2D([0],[0], color=c, linewidth=2, label=d["symbol"])
               for d, c in zip(data_list, colors)]
    ax.legend(handles=handles, fontsize=8, framealpha=0.9,
              loc="upper left", ncol=max(1, len(data_list)//5))


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
    fig = plt.figure(figsize=(16, 10), facecolor="white")
    fig.suptitle(f"{d['symbol']} — {d['name']}  |  Fundamentals {year_range}",
                 fontsize=14, fontweight="bold", y=0.98)

    gs  = gridspec.GridSpec(2, 3, figure=fig, hspace=0.45, wspace=0.38)
    yrs = year_labels(d["years"])
    x   = np.arange(len(yrs))

    # ── Panel 1: Share Price (always line) ────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    ax1.plot(yrs, clean(d["prices"]), color=color, linewidth=2, marker="o", markersize=4)
    if d.get("current_price"):
        ax1.axhline(d["current_price"], color=color, linestyle="--", linewidth=1,
                    label=f"Current ${d['current_price']:,.2f}")
    if d.get("analyst_tp"):
        ax1.axhline(d["analyst_tp"], color="#888", linestyle=":", linewidth=1,
                    label=f"TP ${d['analyst_tp']:,.2f}")
    ax1.set_title("Share Price ($)", fontsize=10, fontweight="bold")
    ax1.set_xticks(x[::2]); ax1.set_xticklabels(yrs[::2], fontsize=8)
    ax1.legend(fontsize=7)
    add_zero_line(ax1)

    # ── Panel 2: P/E & PEG ───────────────────────────────────────────────────
    # Left axis: historical P/E (bar or line per pref)
    # Right axis: historical PEG (always line, orange)
    # Dotted lines: current forward P/E (gold) and current PEG (coral)
    hist_peg = compute_historical_peg(d["pe"], d["eps"])
    s2_pe  = prefs.get("panel2_pe",  "bar")
    s2_peg = prefs.get("panel2_peg", "line")

    ax2 = fig.add_subplot(gs[0, 1])
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
    ax2.set_title("P/E Ratio & PEG", fontsize=10, fontweight="bold")
    ax2.set_xticks(x[::2]); ax2.set_xticklabels(yrs[::2], fontsize=8)
    ax2.tick_params(axis="y", labelsize=8)
    ax2b.tick_params(axis="y", labelsize=8, colors="#F59E0B")
    ax2b.set_ylabel("PEG", fontsize=8, color="#F59E0B")
    ax2.legend(handles=legend_lines, fontsize=7)
    add_zero_line(ax2)

    # ── Panel 3: EPS & ROE ───────────────────────────────────────────────────
    # Left axis: EPS (bar or line per pref)
    # Right axis: ROE % (always line, red)
    s3_eps = prefs.get("panel3_eps", "bar")
    s3_roe = prefs.get("panel3_roe", "line")

    ax3 = fig.add_subplot(gs[0, 2])
    ax3.set_axisbelow(True)
    ax3b = ax3.twinx()
    ax3b.set_axisbelow(True)
    ax3b.grid(False)

    _draw_series(ax3, yrs, x, d["eps"], s3_eps, color, alpha=0.50, label="EPS ($)")
    _draw_series(ax3b, yrs, x, d["roe"], s3_roe, "#EF4444", alpha=0.60, label="ROE (%)", marker="s")

    ax3.set_title("EPS ($) & ROE (%)", fontsize=10, fontweight="bold")
    ax3.set_xticks(x[::2]); ax3.set_xticklabels(yrs[::2], fontsize=8)
    ax3.tick_params(axis="y", labelsize=8)
    ax3b.tick_params(axis="y", labelsize=8, colors="#EF4444")
    ax3b.set_ylabel("ROE %", fontsize=8, color="#EF4444")
    lines3 = [
        Line2D([0],[0], color=color, linewidth=6 if s3_eps=="bar" else 2,
               alpha=0.75 if s3_eps=="bar" else 1.0, label="EPS ($)"),
        Line2D([0],[0], color="#EF4444", linewidth=6 if s3_roe=="bar" else 2,
               alpha=0.75 if s3_roe=="bar" else 1.0, label="ROE (%)"),
    ]
    ax3.legend(handles=lines3, fontsize=7)
    add_zero_line(ax3)

    # ── Panel 4: Book Value/Share & Debt/Assets ───────────────────────────────
    # Left axis: BV/Share (bar or line per pref)
    # Right axis: Debt/Assets % (always line, teal)
    s4_bvps = prefs.get("panel4_bvps", "bar")
    s4_debt = prefs.get("panel4_debt", "line")
    da_pct = [v * 100 if v is not None else None for v in d["debt_assets"]]

    ax4 = fig.add_subplot(gs[1, 0])
    ax4.set_axisbelow(True)
    ax4b = ax4.twinx()
    ax4b.set_axisbelow(True)
    ax4b.grid(False)

    _draw_series(ax4, yrs, x, d["bvps"], s4_bvps, color, alpha=0.50, label="BV/Sh ($)")
    _draw_series(ax4b, yrs, x, da_pct, s4_debt, "#06B6D4", alpha=0.60, label="Debt/Assets (%)")

    ax4.set_title("Book Value/Share & Debt/Assets", fontsize=10, fontweight="bold")
    ax4.set_xticks(x[::2]); ax4.set_xticklabels(yrs[::2], fontsize=8)
    ax4.tick_params(axis="y", labelsize=8)
    ax4b.tick_params(axis="y", labelsize=8, colors="#06B6D4")
    ax4b.set_ylabel("Debt/Assets %", fontsize=8, color="#06B6D4")
    lines4 = [
        Line2D([0],[0], color=color, linewidth=6 if s4_bvps=="bar" else 2,
               alpha=0.75 if s4_bvps=="bar" else 1.0, label="BV/Sh ($)"),
        Line2D([0],[0], color="#06B6D4", linewidth=6 if s4_debt=="bar" else 2,
               alpha=0.75 if s4_debt=="bar" else 1.0, label="Debt/Assets (%)"),
    ]
    ax4.legend(handles=lines4, fontsize=7)
    add_zero_line(ax4)

    # ── Panel 5: OCF/Share & FCF/Share ───────────────────────────────────────
    s5_ocf = prefs.get("panel5_ocf", "bar")
    s5_fcf = prefs.get("panel5_fcf", "line")

    ax5 = fig.add_subplot(gs[1, 1])
    ax5.set_axisbelow(True)
    _draw_series(ax5, yrs, x, d["ocfps"], s5_ocf, color, alpha=0.50, label="OCF/Sh ($)")
    _draw_series(ax5, yrs, x, d["fcfps"], s5_fcf, "#10B981", alpha=0.90, label="FCF/Sh ($)", marker="o", linewidth=2.5)
    ax5.set_title("OCF/Share & FCF/Share ($)", fontsize=10, fontweight="bold")
    ax5.set_xticks(x[::2]); ax5.set_xticklabels(yrs[::2], fontsize=8)
    ax5.tick_params(axis="y", labelsize=8)
    ax5.legend(fontsize=7)
    add_zero_line(ax5)

    # ── Panel 6: Revenue/Share & Div/Share ───────────────────────────────────
    s6_rev = prefs.get("panel6_rev", "bar")
    s6_div = prefs.get("panel6_div", "line")

    ax6 = fig.add_subplot(gs[1, 2])
    ax6.set_axisbelow(True)
    ax6b = ax6.twinx()
    ax6b.set_axisbelow(True)
    ax6b.grid(False)

    _draw_series(ax6, yrs, x, d["revps"], s6_rev, color, alpha=0.50, label="Rev/Sh ($)")
    if any(v and v > 0 for v in d["divps"]):
        _draw_series(ax6b, yrs, x, d["divps"], s6_div, "#7C3AED", alpha=0.80, label="Div/Sh ($)", marker="D")
        ax6b.tick_params(axis="y", labelsize=8, colors="#8B5CF6")
        ax6b.set_ylabel("Div/Sh ($)", fontsize=8, color="#8B5CF6")
    ax6.set_title("Revenue/Share & Div/Share ($)", fontsize=10, fontweight="bold")
    ax6.set_xticks(x[::2]); ax6.set_xticklabels(yrs[::2], fontsize=8)
    ax6.tick_params(axis="y", labelsize=8)
    lines6 = [
        Line2D([0],[0], color=color, linewidth=6 if s6_rev=="bar" else 2,
               alpha=0.75 if s6_rev=="bar" else 1.0, label="Rev/Sh ($)"),
        Line2D([0],[0], color="#8B5CF6", linewidth=6 if s6_div=="bar" else 2,
               alpha=0.75 if s6_div=="bar" else 1.0, label="Div/Sh ($)"),
    ]
    ax6.legend(handles=lines6, fontsize=7)
    add_zero_line(ax6)

    # ── Footer ────────────────────────────────────────────────────────────────
    consensus_str = d.get("consensus", "")
    tp_str  = f"  TP ${d['analyst_tp']:,.2f}" if d.get("analyst_tp") else ""
    low_str = f"  Low ${d['analyst_low']:,.2f}" if d.get("analyst_low") else ""
    hi_str  = f"  High ${d['analyst_high']:,.2f}" if d.get("analyst_high") else ""
    peg_str = f"  PEG {cur_peg:.2f}" if cur_peg else ""
    fig.text(0.5, 0.01,
             f"Analyst Consensus: {consensus_str}{tp_str}{low_str}{hi_str}{peg_str}",
             ha="center", fontsize=9, color="#555", fontfamily="monospace")

    plt.tight_layout(rect=[0, 0.03, 1, 0.97])

    for ax in fig.axes:
        mplcursors.cursor(ax, hover=True)
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# 6. COMPARISON CHARTS
# ─────────────────────────────────────────────────────────────────────────────

def plot_comparison(data_list, colors):
    apply_style()
    fig, axes = plt.subplots(3, 3, figsize=(18, 14), facecolor="white")
    fig.suptitle("All Tickers — Side-by-Side Comparison",
                 fontsize=14, fontweight="bold", y=0.99)

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
        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.set_xticks(x[::2]); ax.set_xticklabels(yrs[::2], fontsize=8, rotation=30)
        ax.tick_params(axis="y", labelsize=8)
        add_zero_line(ax)
        ticker_legend(ax, data_list, colors)

    plt.tight_layout(rect=[0, 0, 1, 0.97], h_pad=3.0)
    for ax in fig.axes:
        mplcursors.cursor(ax, hover=True)
    return fig

def plot_stock_table(data_list, colors, years_back):
    apply_style()

    def eps_cagr(eps_list, years_list):
        pairs = [(y, e) for y, e in zip(years_list, eps_list)
                 if e is not None and e > 0]
        if len(pairs) < 2:
            return None
        n = pairs[-1][0] - pairs[0][0]
        if n <= 0:
            return None
        return round(((pairs[-1][1] / pairs[0][1]) ** (1 / n) - 1) * 100, 1)

    def avg_last_n(values, n):
        clean_vals = [v for v in values if v is not None]
        subset = clean_vals[-n:]
        if not subset:
            return None
        return round(sum(subset) / len(subset), 2)

    def pe_avg_5yr(pe_list):
        return avg_last_n(pe_list, 5)

    col_labels = [
        "Name",
        "Price\n(current)",
        f"EPS CAGR\n({years_back - 1}yr)",
        "Fwd/Avg\nP/E ratio",
        "P/E\n(forward)",
        "P/E\n(trailing)",
        "P/E\n(5yr avg)",
        "FCF/Sh $\n(latest)",
        "FCF/Sh $\n(3yr avg)",
        "ROE %\n(latest)",
        "ROE %\n(3yr avg)",
    ]
    row_labels = [d["symbol"] for d in data_list]

    table_data = []
    cell_colors = []

    for d in data_list:
        row = []
        crow = []

        row.append(d.get("name", "")); crow.append("#EAF4FB")

        cur_price = d.get("current_price")
        if cur_price is None:
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            row.append(f"${float(cur_price):,.2f}"); crow.append("#EAF4FB")

        val = eps_cagr(d["eps"], d["years"])
        if val is None:
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            row.append(f"{val:+.1f}%")
            crow.append("#D4EDDA" if val >= 0 else "#F8D7DA")

        cur_pe = d.get("trailing_pe")
        cur_pe = float(cur_pe) if cur_pe is not None else None
        fwd_pe = d.get("forward_pe")
        fwd_pe = float(fwd_pe) if fwd_pe is not None else None
        avg_pe = pe_avg_5yr(d["pe"])

        if fwd_pe is not None and avg_pe is not None and avg_pe > 0:
            ratio = round(fwd_pe / avg_pe, 2)
            row.append(f"{ratio:.2f}x")
            val_c = "#D4EDDA" if ratio < 0.8 else ("#FFF3CD" if ratio <= 1.1 else "#F8D7DA")
            crow.append(val_c)
        else:
            row.append("N/A"); crow.append("#F0F0F0")

        if fwd_pe is None:
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            row.append(f"{fwd_pe:.1f}x"); crow.append("#FFF9E6")

        if cur_pe is None:
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            row.append(f"{cur_pe:.1f}x"); crow.append("#FFF9E6")

        if avg_pe is None:
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            cell_c = "#FFF9E6"
            if cur_pe is not None:
                cell_c = "#D4EDDA" if cur_pe < avg_pe else "#F8D7DA"
            row.append(f"{avg_pe:.1f}x"); crow.append(cell_c)

        fcf_lat = latest(d["fcfps"])
        if np.isnan(fcf_lat):
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            row.append(f"${fcf_lat:.2f}")
            crow.append("#D4EDDA" if fcf_lat >= 0 else "#F8D7DA")

        fcf_avg = avg_last_n(d["fcfps"], 3)
        if fcf_avg is None:
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            row.append(f"${fcf_avg:.2f}")
            crow.append("#D4EDDA" if fcf_avg >= 0 else "#F8D7DA")

        roe_lat = latest(d["roe"])
        if np.isnan(roe_lat):
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            row.append(f"{roe_lat:.1f}%")
            crow.append("#D4EDDA" if roe_lat >= 15 else ("#FFF3CD" if roe_lat >= 8 else "#F8D7DA"))

        roe_avg = avg_last_n(d["roe"], 3)
        if roe_avg is None:
            row.append("N/A"); crow.append("#F0F0F0")
        else:
            row.append(f"{roe_avg:.1f}%")
            crow.append("#D4EDDA" if roe_avg >= 15 else ("#FFF3CD" if roe_avg >= 8 else "#F8D7DA"))

        table_data.append(row)
        cell_colors.append(crow)

    fig_height = max(4.5, 2.0 + len(data_list) * 0.9)
    fig, ax = plt.subplots(figsize=(14, fig_height), facecolor="white")
    fig.suptitle("Stock Scorecard", fontsize=13, fontweight="bold", y=0.95)
    ax.axis("off")

    table = ax.table(
        cellText=table_data,
        rowLabels=row_labels,
        colLabels=col_labels,
        cellColours=cell_colors,
        cellLoc="center",
        rowLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1.0, 2.4)

    for col_idx in range(len(col_labels)):
        table.auto_set_column_width(col_idx)

    for (row_idx, col_idx), cell in table.get_celld().items():
        cell.set_edgecolor("#CCCCCC")
        if row_idx == 0 or col_idx == -1:
            cell.set_facecolor("#E8F4FD")
            cell.set_text_props(fontweight="bold")
        if row_idx > 0 and col_idx >= 0:
            text = cell.get_text().get_text()
            if text == "N/A":
                cell.get_text().set_color("#AAAAAA")

    plt.tight_layout(rect=[0, 0, 1, 0.88])
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

    fig, axes = plt.subplots(2, 4, figsize=(18, 9), facecolor="white")
    fig.suptitle("Latest Year — All Tickers Snapshot",
                 fontsize=14, fontweight="bold", y=1.00)

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
        ax.set_xticklabels(syms, fontsize=11, fontweight="bold", rotation=45, ha="right")
        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.tick_params(axis="y", labelsize=8)
        add_zero_line(ax)
        ax.grid(False)

        for xi, v in enumerate(vals_plot):
            if v == 0:
                continue
            label = f"{v:.1f}" if abs(v) < 1000 else f"{v/1000:.1f}k"
            ax.text(xi, v + (max(vals_plot) * 0.02 if v >= 0 else min(vals_plot) * 0.02),
                    label, ha="center", va="bottom" if v >= 0 else "top",
                    fontsize=11, fontweight="bold", fontfamily="monospace")

    plt.tight_layout()
    for ax in fig.axes:
        mplcursors.cursor(ax, hover=True)
    return fig

def cagr(prices, n):
    clean_prices = [p for p in prices if p is not None]
    if len(clean_prices) < n + 1:
        return None
    end   = clean_prices[-1]
    start = clean_prices[-(n + 1)]
    if start is None or end is None or start <= 0:
        return None
    return round(((end / start) ** (1 / n) - 1) * 100, 2)


def plot_etf(etf_list, colors, years_back):
    apply_style()
    n = len(etf_list)

    fig = plt.figure(figsize=(18, 10), facecolor="white")
    fig.suptitle("ETF Overview — Price, Distributions & Annual Return",
                 fontsize=14, fontweight="bold", y=0.98)

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
    ax0.set_title("Price ($)", fontsize=10, fontweight="bold")
    ax0.set_xticks(x[::2]); ax0.set_xticklabels(yrs[::2], fontsize=8, rotation=30)
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
    ax1.set_title("Annual Return (%)", fontsize=10, fontweight="bold")
    ax1.set_xticks(x[::2]); ax1.set_xticklabels(yrs[::2], fontsize=8, rotation=30)
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
    ax2.set_title("Annual Distributions ($)", fontsize=10, fontweight="bold")
    ax2.set_xticks(x[::2]); ax2.set_xticklabels(yrs[::2], fontsize=8, rotation=30)
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
    ax3.set_title("Cumulative Total Return (Base = 100)", fontsize=10, fontweight="bold")
    ax3.set_xticks(x[::2]); ax3.set_xticklabels(yrs[::2], fontsize=8, rotation=30)
    ticker_legend(ax3, etf_list, colors)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    for ax in [ax0, ax1, ax2, ax3]:
        mplcursors.cursor(ax, hover=True)
    return fig

def plot_etf_table(etf_list, colors, years_back):
    apply_style()

    periods = sorted(set([1, 3, 5, 10, years_back - 1]))

    col_labels = (
            ["Name"]
            + [f"CAGR {p}yr" for p in periods]
        + ["Best Year", "Worst Year", "Avg Return", "Volatility", "Total Return", "Yield %"]
    )
    row_labels = [d["symbol"] for d in etf_list]

    table_data = []
    cell_colors = []

    for d in etf_list:
        row = []
        colors_row = []
        row.append(d.get("name", "")); colors_row.append("#EAF4FB")

        for p in periods:
            val = cagr(d["prices"], p)
            if val is None:
                row.append("N/A"); colors_row.append("#F0F0F0")
            else:
                row.append(f"{val:+.1f}%")
                colors_row.append("#D4EDDA" if val >= 0 else "#F8D7DA")

        valid_returns = [(yr, r) for yr, r in zip(d["years"], d["annual_returns"])
                         if r is not None]

        if valid_returns:
            best_yr, best_val = max(valid_returns, key=lambda x: x[1])
            row.append(f"{best_yr}  {best_val:+.1f}%"); colors_row.append("#D4EDDA")
        else:
            row.append("N/A"); colors_row.append("#F0F0F0")

        if valid_returns:
            worst_yr, worst_val = min(valid_returns, key=lambda x: x[1])
            row.append(f"{worst_yr}  {worst_val:+.1f}%"); colors_row.append("#F8D7DA")
        else:
            row.append("N/A"); colors_row.append("#F0F0F0")

        if valid_returns:
            avg = round(sum(r for _, r in valid_returns) / len(valid_returns), 1)
            row.append(f"{avg:+.1f}%")
            colors_row.append("#D4EDDA" if avg >= 0 else "#F8D7DA")
        else:
            row.append("N/A"); colors_row.append("#F0F0F0")

        if len(valid_returns) >= 2:
            ret_vals = [r for _, r in valid_returns]
            vol = round(float(np.std(ret_vals, ddof=1)), 1)
            row.append(f"{vol:.1f}%")
            vol_color = "#D4EDDA" if vol < 12 else ("#FFF3CD" if vol < 20 else "#F8D7DA")
            colors_row.append(vol_color)
        else:
            row.append("N/A"); colors_row.append("#F0F0F0")

        # Total return — uses only the trimmed window (already sliced to years_back)
        clean_prices = [p for p in d["prices"] if p is not None]
        if len(clean_prices) >= 2:
            total_ret = round((clean_prices[-1] / clean_prices[0] - 1) * 100, 1)
            row.append(f"{total_ret:+.0f}%")
            colors_row.append("#D4EDDA" if total_ret >= 0 else "#F8D7DA")
        else:
            row.append("N/A"); colors_row.append("#F0F0F0")

        try:
            latest_dist = next(
                (d["distributions"][i] for i in range(len(d["years"]) - 1, -1, -1)
                 if d["distributions"][i] is not None and d["distributions"][i] > 0),
                None
            )
            cur_price = d.get("current_price")
            if latest_dist and cur_price and cur_price > 0:
                yield_pct = round(latest_dist / cur_price * 100, 2)
                row.append(f"{yield_pct:.2f}%"); colors_row.append("#EAF4FB")
            else:
                row.append("N/A"); colors_row.append("#F0F0F0")
        except Exception:
            row.append("N/A"); colors_row.append("#F0F0F0")

        table_data.append(row)
        cell_colors.append(colors_row)

    fig_height = max(4.5, 2.0 + len(etf_list) * 0.9)
    fig, ax = plt.subplots(figsize=(18, fig_height), facecolor="white")
    fig.suptitle("ETF Performance Summary", fontsize=13, fontweight="bold", y=0.95)
    ax.axis("off")

    table = ax.table(
        cellText=table_data,
        rowLabels=row_labels,
        colLabels=col_labels,
        cellColours=cell_colors,
        cellLoc="center",
        rowLoc="center",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1.0, 2.2)

    for col_idx in range(len(col_labels)):
        table.auto_set_column_width(col_idx)

    for (row_idx, col_idx), cell in table.get_celld().items():
        cell.set_edgecolor("#CCCCCC")
        if row_idx == 0 or col_idx == -1:
            cell.set_facecolor("#E8F4FD")
            cell.set_text_props(fontweight="bold")
        if row_idx > 0 and col_idx >= 0:
            text = cell.get_text().get_text()
            if text == "N/A":
                cell.get_text().set_color("#AAAAAA")
            elif "+" in text:
                cell.get_text().set_color("#155724")
            elif text and text[0] == "-":
                cell.get_text().set_color("#721C24")

    plt.tight_layout(rect=[0, 0, 1, 0.88])
    return fig

def export_session(stock_list, stock_colors, etf_list, etf_colors,
                   figs_stock_single, fig_comparison, fig_snapshot,
                   fig_etf, fig_etf_table, fig_stock_table, years_back):
    today = datetime.now().strftime("%Y-%m-%d")
    out   = make_session_folder(stock_list, etf_list)
    saved = []

    if stock_list:
        path = os.path.join(out, f"{today}_scorecard.csv")

        def avg_last_n(values, n):
            clean_vals = [v for v in values if v is not None]
            subset = clean_vals[-n:]
            return round(sum(subset) / len(subset), 2) if subset else None

        def eps_cagr(eps_list, years_list):
            pairs = [(y, e) for y, e in zip(years_list, eps_list)
                     if e is not None and e > 0]
            if len(pairs) < 2:
                return None
            n = pairs[-1][0] - pairs[0][0]
            if n <= 0:
                return None
            return round(((pairs[-1][1] / pairs[0][1]) ** (1 / n) - 1) * 100, 1)

        fieldnames = [
            "symbol", "name", "price",
            f"eps_cagr_{years_back-1}yr",
            "trailing_pe", "forward_pe", "pe_5yr_avg", "fwd_vs_avg_pe",
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
                writer.writerow({
                    "symbol":                   d["symbol"],
                    "name":                     d["name"],
                    "price":                    d.get("current_price"),
                    f"eps_cagr_{years_back-1}yr": eps_cagr(d["eps"], d["years"]),
                    "trailing_pe":              cur_pe,
                    "forward_pe":               fwd_pe,
                    "pe_5yr_avg":               avg_pe,
                    "fwd_vs_avg_pe":            fwd_avg,
                    "roe_latest":               latest(d["roe"]) if not np.isnan(latest(d["roe"])) else None,
                    "roe_3yr_avg":              avg_last_n(d["roe"], 3),
                    "fcfps_latest":             latest(d["fcfps"]) if not np.isnan(latest(d["fcfps"])) else None,
                    "fcfps_3yr_avg":            avg_last_n(d["fcfps"], 3),
                })
        saved.append(f"{today}_scorecard.csv")

    if stock_list:
        path = os.path.join(out, f"{today}_ticker_detail.csv")

        fieldnames = [
            "symbol", "name", "fiscal_year",
            # Per-year series (everything in the charts)
            "price", "eps", "pe", "historical_peg",
            "roe", "bvps", "debt_assets_pct",
            "ocfps", "fcfps", "revps", "divps",
            # Current / analyst data (repeated per row for easy filtering)
            "current_price", "trailing_pe", "forward_pe", "peg_ratio",
            "analyst_tp", "analyst_low", "analyst_high", "consensus",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for d in stock_list:
                hist_peg = compute_historical_peg(d["pe"], d["eps"])

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

    pairs = [
        (fig_stock_table, "scorecard"),
        (fig_comparison,  "comparison"),
        (fig_snapshot,    "snapshot"),
        (fig_etf,         "etf_overview"),
        (fig_etf_table,   "etf_table"),
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

    # ── Header ──────────────────────────────────────────────────────────────
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

    # ── Series rows ─────────────────────────────────────────────────────────
    frame = tk.Frame(dlg, bg=BG, padx=24, pady=8)
    frame.pack(fill="x")

    # Column headers
    tk.Label(frame, text="Series", bg=BG, fg="#888",
             font=("Segoe UI", 10), anchor="w").grid(
        row=0, column=0, sticky="w", padx=(0, 16))
    for ci, lbl in enumerate(("Bar", "Line"), start=1):
        tk.Label(frame, text=lbl, bg=BG, fg="#888",
                 font=("Segoe UI", 10)).grid(row=0, column=ci, padx=10)

    vars_ = {}
    grid_row = 1

    for key, (panel_title, series_label) in PANEL_LABELS.items():
        # Panel group heading row
        if panel_title is not None:
            tk.Label(
                frame, text=panel_title,
                bg=BG, fg="#1A1A2E",
                font=FONT_PANEL,
                anchor="w",
            ).grid(row=grid_row, column=0, columnspan=3, sticky="w",
                   pady=(12, 2))
            grid_row += 1

        # Series label
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

    # ── Separator ───────────────────────────────────────────────────────────
    tk.Frame(dlg, bg="#CCCCCC", height=1).pack(fill="x", padx=24, pady=(8, 0))

    # ── Buttons ─────────────────────────────────────────────────────────────
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

    # Centre over parent
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
        chart_prefs       = load_chart_prefs()
        figs_stock_single = []
        fig_stock_table   = None
        fig_comparison    = None
        fig_snapshot      = None
        fig_etf           = None
        fig_etf_table     = None

        if need_charts:
            for d, col in zip(stock_list, stock_colors):
                log(f"  Chart: {d['symbol']}")
                fig = plot_single_ticker(d, col, prefs=chart_prefs)
                fig.canvas.manager.set_window_title(f"{d['symbol']} \u2014 {d['name']}")
                figs_stock_single.append(fig)

            if stock_list:
                log("  Table: Stock Scorecard")
                show_stock_table(stock_list, stock_colors, YEARS_BACK)

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

        figs = figs_stock_single[:]
        for f in [fig_stock_table, fig_comparison, fig_snapshot, fig_etf, fig_etf_table]:
            if f is not None:
                figs.append(f)

        if do_export:
            log("\nExporting session files\u2026", title="Exporting\u2026")
            saved, session_folder = export_session(
                stock_list, stock_colors, etf_list, etf_colors,
                figs_stock_single, fig_comparison, fig_snapshot,
                fig_etf, fig_etf_table, fig_stock_table, YEARS_BACK,
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

        # Charts closed — wait for Run Again or window close.
        # _run_again() will update _run_state and call root.quit().
        # _go() will also call root.quit() after updating _run_state.
        try:
            if _root is not None and _root.winfo_exists():
                _title_var.set("Done \u2014 run again?")
                _root.mainloop()
        except Exception:
            pass

        # Read whatever _run_again / _go put into the shared state dict
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