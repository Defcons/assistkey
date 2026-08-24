"""Central logging + crash capture so field issues are diagnosable from assistkey.log.

One rotating file (assistkey.log, plus .1/.2/.3), every line timestamped with a level
and thread name, and hooks that catch otherwise-invisible crashes in EVERY thread —
main, the asyncio loop, the pynput hotkey listener, the pystray tray — as well as any
stray stdout/stderr (there is no console under pythonw, so uncaught output vanishes
otherwise). The access token is never written.

Usage: call `setup()` once at startup, then `logging.getLogger("assistkey.<area>")`
anywhere. `log_config(config)` records a redacted one-line snapshot for context.
"""

from __future__ import annotations

import logging
import logging.handlers
import platform
import sys
import threading
import urllib.parse
from pathlib import Path

LOG_PATH = Path(__file__).with_name("assistkey.log")
log = logging.getLogger("assistkey")

REPO_URL = "https://github.com/Defcons/assistkey"

# Deliberately generic: nothing here is specific to one user or one machine — no
# log content, no config values, no URL, no file paths. Just software-version
# facts (same for every install) so the draft stays genuinely anonymous. The user
# attaches their own log file by hand if and when they choose to.
_ISSUE_TEMPLATE = """### What were you doing?
<!-- e.g. "Pressed the hotkey and started talking" -->


### What did you expect to happen, and what happened instead?


### Log (optional)
If it helps, attach your **assistkey.log** by dragging it into this box —
tray icon → **Open log** finds the file. Nothing is attached automatically.

### System
- AssistKey: {mode}
- Python: {python_version}
- OS: {os_version}
"""

_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 1_000_000
_BACKUPS = 3


class _StreamToLogger:
    """File-like shim so a stray print() or a library's stderr still lands in the log."""

    def __init__(self, level: int):
        self._level = level
        self._buf = ""

    def write(self, msg):
        try:
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    log.log(self._level, line.rstrip())
        except Exception:  # noqa: BLE001 - logging must never raise back into the caller
            pass

    def flush(self):
        try:
            if self._buf.strip():
                log.log(self._level, self._buf.rstrip())
            self._buf = ""
        except Exception:  # noqa: BLE001
            pass

    def isatty(self):
        return False


def _make_handler(path: Path) -> logging.Handler:
    h = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8", delay=True)
    h.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    return h


def redact_config(config) -> str:
    """One-line config snapshot for the log. The token is shown only as set/none."""
    try:
        url, token = config.credentials()
    except Exception:  # noqa: BLE001
        url, token = "?", ""
    return (f"url={url or '(none)'} token={'set' if token else '(none)'} "
            f"hotkey={list(config.hotkey)} mode={config.trigger_mode} "
            f"wake={config.wake_enabled}/{config.wake_word} follow_up={config.follow_up_enabled} "
            f"mic={config.mic_device} spk={config.speaker_device} monitor={config.popup_monitor}")


def log_config(config) -> None:
    log.info("config: %s", redact_config(config))


def build_issue_url(repo_url: str = REPO_URL) -> str:
    """A GitHub 'new issue' URL prefilled with a blank repro template and generic
    software-version info only — no log content, no config, no URL, nothing that
    could identify the reporter or their setup. For the user to fill in and submit
    themselves (their GitHub login creates it; nothing is sent automatically)."""
    body = _ISSUE_TEMPLATE.format(
        mode="packaged .exe" if getattr(sys, "frozen", False) else "source (python)",
        python_version=platform.python_version(),
        os_version=platform.platform(),
    )
    query = urllib.parse.urlencode({"title": "Bug: ", "body": body, "labels": "bug"})
    return f"{repo_url}/issues/new?{query}"


def asyncio_exception_handler(loop, context) -> None:
    """Log an unhandled exception from an asyncio task (would otherwise be silent)."""
    exc = context.get("exception")
    msg = context.get("message", "")
    if exc is not None:
        log.error("asyncio: %s", msg, exc_info=exc)
    else:
        log.error("asyncio: %s", msg)


def _install_hooks() -> None:
    def _main(exc_type, exc, tb):
        log.critical("UNCAUGHT exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = _main

    def _thread(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.critical("UNCAUGHT exception in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _thread


def setup(path: Path = LOG_PATH, capture_streams: bool = True, install_hooks: bool = True) -> None:
    """Wire the rotating file log + crash hooks. Best-effort — never blocks startup."""
    try:
        log.setLevel(logging.DEBUG)
        log.handlers.clear()
        log.addHandler(_make_handler(path))
        log.propagate = False
        if capture_streams:
            sys.stdout = _StreamToLogger(logging.INFO)
            sys.stderr = _StreamToLogger(logging.ERROR)
        if install_hooks:
            _install_hooks()
        log.info("---- session start ---- AssistKey  python %s  %s",
                 platform.python_version(), platform.platform())
    except Exception:  # noqa: BLE001 - logging setup must never crash the app
        pass
