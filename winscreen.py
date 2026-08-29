"""Windows monitor/work-area + dark-titlebar helpers (ctypes, no dependencies).

Shared by the popup overlay (which places itself on a chosen monitor) and the
settings dialog (which caps its height to the work area). Everything degrades
gracefully off-Windows: helpers return None / a sane default and the dark
titlebar is simply skipped.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def apply_dark_titlebar(win):
    """Make a window's native title bar dark (Windows DWM) — no white bar."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20 newer, 19 older builds)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def monitor_workarea_at(x: int, y: int):
    """Work-area rect (left, top, right, bottom) of the monitor under a point.

    Uses the Windows monitor under the given screen coords so the toast lands on
    whichever display the user is on. Returns None if the API call fails.
    """
    try:
        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # NEAREST
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001 - fall back to primary screen
        return None


def list_monitors():
    """All monitors as dicts {primary: bool, work: (l, t, r, b)}, in enum order."""
    result = []
    try:
        user32 = ctypes.windll.user32
        proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

        def _cb(hmon, _hdc, _lprc, _lparam):
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                w = mi.rcWork
                result.append({"primary": bool(mi.dwFlags & 1),
                               "work": (w.left, w.top, w.right, w.bottom)})
            return 1

        user32.EnumDisplayMonitors(None, None, proc(_cb), 0)
    except Exception:  # noqa: BLE001
        pass
    return result


def primary_workarea():
    for m in list_monitors():
        if m["primary"]:
            return m["work"]
    try:
        gsm = ctypes.windll.user32.GetSystemMetrics
        return (0, 0, gsm(0), gsm(1) - 48)
    except Exception:  # noqa: BLE001
        return (0, 0, 1920, 1032)
