"""
app_settings.py
────────────────
Shared "Display Preferences" store for the IT Fundamentals Dashboard.

Holds the user-adjustable settings that used to be hardcoded:
  • picker_font_scale     — font size (%) for the Ticker Picker window
  • scorecard_font_scale  — font size (%) for the scorecard / table windows
  • chart_font_scale      — font size (%) for matplotlib chart text
  • default_years_back    — default value pre-filled in the "History: __ yrs" box

Persisted to tickers/app_settings.ini (plain INI, edited via Edit ▸
Preferences ▸ Display Settings… in ticker_picker.py).

Deliberately has NO dependency on main.py / ticker_picker.py / interactive_table.py
so all three can import it without circular-import issues.
"""

import configparser
import os
import sys

# ── Path resolution (mirrors main.py's tickers/ folder logic) ────────────────
_BASE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
DB_DIR        = os.path.join(_BASE, "tickers")
SETTINGS_PATH = os.path.join(DB_DIR, "app_settings.ini")

DEFAULTS = {
    "picker_font_scale":    "100",   # %
    "scorecard_font_scale": "100",   # %
    "chart_font_scale":     "100",   # %
    "default_years_back":   "4",
}

# Slider range used by the Display Settings dialog
SCALE_MIN = 50
SCALE_MAX = 200


def load_settings() -> dict:
    """Load settings from disk, falling back to DEFAULTS for anything missing/bad."""
    settings = dict(DEFAULTS)
    if os.path.exists(SETTINGS_PATH):
        cfg = configparser.ConfigParser()
        try:
            cfg.read(SETTINGS_PATH)
        except Exception:
            return settings
        if "display" in cfg:
            for key in DEFAULTS:
                if key in cfg["display"]:
                    settings[key] = cfg["display"][key]
    return settings


def save_settings(settings: dict) -> None:
    """Write settings back to tickers/app_settings.ini."""
    os.makedirs(DB_DIR, exist_ok=True)
    cfg = configparser.ConfigParser()
    cfg["display"] = {k: str(v) for k, v in settings.items()}
    with open(SETTINGS_PATH, "w") as f:
        cfg.write(f)


def get_float(settings: dict, key: str, default: float = 100.0) -> float:
    try:
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def get_int(settings: dict, key: str, default: int = 5) -> int:
    try:
        return max(1, int(float(settings.get(key, default))))
    except (TypeError, ValueError):
        return default


def scaled_size(base_size, scale_pct, minimum=6):
    """Scale a base font/pixel size by a percentage, with a sane floor."""
    try:
        return max(minimum, round(base_size * float(scale_pct) / 100.0))
    except (TypeError, ValueError):
        return base_size
