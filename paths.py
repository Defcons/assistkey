"""Where AssistKey keeps its user-writable files (config.json, assistkey.log).

Source run: the source directory. Frozen (PyInstaller one-file) build: the folder
that CONTAINS AssistKey.exe — deliberately NOT the ephemeral `_MEIPASS` extraction
dir that `__file__` resolves into inside a frozen build (that dir is wiped on exit,
so config + logs written there would vanish on every restart). Keeping them next to
the exe means settings persist and tray → "Open log" finds the file.
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Directory for user files (config, log) — next to the app, writable, persistent."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent   # folder holding AssistKey.exe
    return Path(__file__).resolve().parent             # source tree
