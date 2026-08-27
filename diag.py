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

# Patterns of things that can end up inside an exception message/traceback and
# would identify the reporter or their network if posted verbatim to a PUBLIC
# issue: any URL, a JWT-shaped string (HA long-lived tokens look like this —
# defence in depth, since nothing currently logs the token), a Windows user
# profile path, and IPv4 addresses.
_URL_RE = re.compile(r"https?://\S+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_WIN_USER_RE = re.compile(r"(C:\\Users\\)[^\\\s]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Deliberately minimal beyond the excerpt: no config dump, no raw file paths, no
# raw log line count. Everything reaching the excerpt slot has already gone
# through `_redact` — but a human glance is still the last line of defence, since
# no regex catches everything a future log line might contain.
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


def _redact(text: str, config=None) -> str:
    """Strip privacy-sensitive substrings from log text before it goes into a
    PUBLIC issue draft. Two passes: an exact replace of the user's OWN configured
    Home Assistant URL/host (most precise — labels it clearly), then generic
    patterns for anything else that slipped in (a different URL, a token-shaped
    string, a Windows user path, an IPv4 address). Errs toward over-redacting —
    losing a little context is fine, leaking an address or a path isn't.
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


def find_error_excerpt(paths, max_chars: int = MAX_EXCERPT_CHARS) -> str:
    """The most recent ERROR/CRITICAL log record (+ any traceback under it) found
    across `paths`, checked newest-file-first. A record's traceback lines have no
    timestamp, so a block runs until the next timestamped line or EOF.

    Falls back to the tail of the first non-empty log if nothing reached
    ERROR/CRITICAL — routine WARNING noise (a reconnect retry, say) is skipped in
    favour of a real crash whenever one exists. Returned text is NOT yet redacted
    — callers must pass it through `_redact` before it leaves the machine.
    """
    for path in paths:
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
    for path in paths:
        if path.exists() and path.stat().st_size > 0:
            return _clip(_tail(path), max_chars)
    return ""


def build_issue_url(config=None, log_dir: Path | None = None, repo_url: str = REPO_URL) -> str:
    """A GitHub 'new issue' URL prefilled with a title and the most recent error
    from the log, run through `_redact` so the excerpt carries no HA URL/host, no
    other URL, no token-shaped string, no Windows user path, and no IP address.
    For the user to review and submit themselves (their GitHub login creates it;
    nothing is sent automatically)."""
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
