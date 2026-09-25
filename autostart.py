r"""Start-at-login toggle using the per-user Run registry key (no admin needed).

Windows starts everything under HKCU\...\Run at sign-in. The packaged exe is
registered directly. From source we register the silent VBS launcher via
wscript, the same as double-clicking AssistKey.vbs.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import paths

log = logging.getLogger("assistkey.autostart")

try:
    import winreg
except ImportError:  # noqa: BLE001 - non-Windows: autostart is a no-op
    winreg = None

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "AssistKey"


def _launch_command() -> str:
    """The quoted command for the registry: the exe itself in the packaged build
    (it has no console), or `wscript AssistKey.vbs` when running from source."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    vbs = paths.app_dir() / "AssistKey.vbs"
    windir = os.environ.get("WINDIR", r"C:\Windows")
    wscript = Path(windir) / "System32" / "wscript.exe"
    return f'"{wscript}" "{vbs}"'


def is_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    """Add or remove the Run-key entry. Best-effort; never raises to the caller."""
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, _APP_NAME)
                except FileNotFoundError:
                    pass  # already absent
    except OSError:
        # Saving must not fail over this, but log it so it isn't silent.
        log.exception("could not update the start-at-login registry entry")
