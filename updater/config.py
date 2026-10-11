"""App settings — load/save through the verified v2 configuration layer.

Config lives at %APPDATA%/ps3-rpcs3-game-updater/config.ini (Windows) with keys:
    [paths] downloads, rpcs3   [settings] max_downloads
"""
from __future__ import annotations

import os
import sys

try:  # project root must be importable (dev runs from any cwd / frozen builds)
    import ps3_rpcs3_game_updater as _orig
except ImportError:  # pragma: no cover - path fallback for unusual launch dirs
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import ps3_rpcs3_game_updater as _orig


def load() -> dict:
    """Return {'downloads', 'rpcs3', 'max_downloads'} with verified defaults."""
    return _orig.load_config()


def save(cfg: dict):
    _orig.save_config(cfg)


def config_dir():
    """The verified app data folder (where config.ini and errors.log live)."""
    return _orig.config_dir()


def default_downloads_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "PS3Updates")


def art_cache_dir(downloads_dir: str = "") -> str:
    """Where cached remote artwork lives (an 'artwork' folder next to downloads)."""
    base = downloads_dir or default_downloads_dir()
    d = os.path.join(base, "artwork")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:  # pragma: no cover - read-only FS; art cache is optional
        pass
    return d
