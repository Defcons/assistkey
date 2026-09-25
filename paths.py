"""Where the app keeps config.json and assistkey.log.

From source that's the source folder. In the PyInstaller one-file build it's the
folder containing AssistKey.exe. It must not be `__file__`'s folder there: that
is a temporary extraction dir that is deleted on exit, so settings and logs
would be lost on every restart.
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Folder for the config and log files, next to the app."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent   # folder holding AssistKey.exe
    return Path(__file__).resolve().parent             # source tree
