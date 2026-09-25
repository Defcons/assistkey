"""Logging and crash capture, so problems can be diagnosed from assistkey.log.

One rotating log file (assistkey.log plus .1/.2/.3) with a timestamp, level and
thread name on every line. Hooks catch uncaught exceptions in every thread (main,
asyncio loop, hotkey listener, tray) and stray stdout/stderr, since pythonw has
no console. The access token is never logged.

Call `setup()` once at startup, then use `logging.getLogger("assistkey.<area>")`.
`log_config(config)` logs a one-line summary with the token hidden.
"""

from __future__ import annotations

import logging
import logging.handlers
import platform
import re
import sys
import threading
import urllib.parse
from pathlib import Path

import paths

LOG_PATH = paths.app_dir() / "assistkey.log"
log = logging.getLogger("assistkey")

REPO_URL = "https://github.com/Defcons/assistkey"
MAX_EXCERPT_CHARS = 1500      # keeps the prefilled issue URL a sane length
TAIL_CHARS = 4000             # fallback excerpt when nothing rose to ERROR/CRITICAL

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")
_ERR_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (ERROR|CRITICAL)\s")

# Things that can end up in an exception message and would identify the user or
# their network in a public issue: URLs, JWT-shaped strings (HA tokens look like
# this), Windows user profile paths and IPv4 addresses.
_URL_RE = re.compile(r"https?://\S+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_WIN_USER_RE = re.compile(r"(C:\\Users\\)[^\\\s]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# The body holds only the redacted excerpt and basic system info. The user still
# reviews it before submitting, since no regex catches everything.
_ISSUE_TEMPLATE = """### What were you doing?
<!-- e.g. "Pressed the hotkey and started talking" -->


### What did you expect to happen, and what happened instead?


### Log excerpt (most recent error, auto-redacted)
Personal-looking data (your Home Assistant address, file paths, IP addresses)
has been stripped below automatically — but this is still a PUBLIC issue, so
please give it a quick look before you submit. Attach the full **assistkey.log**
yourself (tray → Open log) if you want to share more.
```
{excerpt}
```

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
        self._lock = threading.Lock()   # any thread may print; keep lines whole

    def write(self, msg):
        try:
            with self._lock:
                self._buf += msg
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    if line.strip():
                        log.log(self._level, line.rstrip())
        except Exception:  # noqa: BLE001 - logging must never raise back into the caller
            pass

    def flush(self):
        try:
            with self._lock:
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


def _redact(text: str, config=None) -> str:
    """Remove personal details from log text before it goes into a public issue:
    first the user's own HA URL and host, then generic patterns (other URLs,
    token-shaped strings, Windows user paths, IPv4 addresses). Errs on the side
    of removing too much.
    """
    if config is not None:
        try:
            url, _ = config.credentials()
        except Exception:  # noqa: BLE001
            url = ""
        if url:
            text = text.replace(url, "[home-assistant-url]")
            host = urllib.parse.urlsplit(url).netloc
            if host:
                text = text.replace(host, "[home-assistant-host]")
    text = _JWT_RE.sub("[token]", text)
    text = _URL_RE.sub("[url]", text)
    text = _WIN_USER_RE.sub(r"\1[user]", text)
    text = _IPV4_RE.sub("[ip]", text)
    return text


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "… (truncated — attach assistkey.log for the full trace)\n" + text[-max_chars:]


def _tail(path: Path, chars: int = TAIL_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-chars:] if len(text) > chars else text


def find_error_excerpt(candidates, max_chars: int = MAX_EXCERPT_CHARS) -> str:
    """The most recent ERROR/CRITICAL record and its traceback, searching the
    newest file first. Traceback lines have no timestamp, so a record runs until
    the next timestamped line.

    If there is no error at all, returns the tail of the first non-empty log.
    The result is not redacted yet: pass it through `_redact` before it leaves
    the machine.
    """
    for path in candidates:
        try:
            if not path.exists() or path.stat().st_size == 0:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        start = next((i for i in range(len(lines) - 1, -1, -1) if _ERR_RE.match(lines[i])), None)
        if start is None:
            continue
        end = start + 1
        while end < len(lines) and not _TS_RE.match(lines[end]):
            end += 1
        return _clip("\n".join(lines[start:end]), max_chars)
    for path in candidates:
        if path.exists() and path.stat().st_size > 0:
            return _clip(_tail(path), max_chars)
    return ""


def build_issue_url(config=None, log_dir: Path | None = None, repo_url: str = REPO_URL) -> str:
    """A GitHub "new issue" URL prefilled with the most recent error from the log,
    redacted with `_redact`. The user reviews and submits it from their own
    account; nothing is sent automatically."""
    log_dir = log_dir or LOG_PATH.parent
    candidates = [log_dir / "assistkey.log", log_dir / "assistkey.log.1"]
    raw = find_error_excerpt(candidates)
    excerpt = _redact(raw, config) if raw else "(no errors logged — describe the issue above)"
    body = _ISSUE_TEMPLATE.format(
        excerpt=excerpt,
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
    """Set up the log file and crash hooks. Never raises, so it can't block startup."""
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
