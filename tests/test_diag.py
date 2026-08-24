"""Diagnostics logging: token redaction, stray-output capture, file output, crash capture."""
import logging
import sys
import tempfile
import threading
import types
import urllib.parse
from pathlib import Path

import config as cfg
import diag


def test_redact_config_never_contains_the_token():
    c = cfg.Config(ha_url="http://x:8123", ha_token="SUPER_SECRET_TOKEN")
    s = diag.redact_config(c)
    assert "SUPER_SECRET_TOKEN" not in s   # the token must never reach the log
    assert "token=set" in s
    assert "url=http://x:8123" in s


def test_redact_config_reports_missing_token(monkeypatch):
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    monkeypatch.delenv("HASS_SERVER", raising=False)
    assert "token=(none)" in diag.redact_config(cfg.Config())


def test_stream_to_logger_routes_whole_lines():
    records = []
    h = logging.Handler()
    h.emit = lambda r: records.append(r.getMessage())
    diag.log.addHandler(h)
    lvl = diag.log.level
    diag.log.setLevel(logging.DEBUG)
    try:
        diag._StreamToLogger(logging.ERROR).write("line one\nline two\n")
        assert records == ["line one", "line two"]
    finally:
        diag.log.removeHandler(h)
        diag.log.setLevel(lvl)


def _teardown_handlers():
    for h in list(diag.log.handlers):
        h.close()
        diag.log.removeHandler(h)


def test_setup_writes_session_banner_and_messages(tmp_path):
    p = tmp_path / "assistkey.log"
    diag.setup(path=p, capture_streams=False, install_hooks=False)
    try:
        logging.getLogger("assistkey.test").info("probe-line-xyz")
        for h in diag.log.handlers:
            h.flush()
        text = p.read_text(encoding="utf-8")
        assert "session start" in text
        assert "probe-line-xyz" in text
    finally:
        _teardown_handlers()


def test_thread_crash_is_captured(tmp_path):
    p = tmp_path / "assistkey.log"
    saved = (sys.excepthook, threading.excepthook)
    diag.setup(path=p, capture_streams=False, install_hooks=True)
    try:
        try:
            raise ValueError("boom-in-thread")
        except ValueError:
            et, ev, tb = sys.exc_info()
        threading.excepthook(types.SimpleNamespace(
            exc_type=et, exc_value=ev, exc_traceback=tb, thread=threading.current_thread()))
        for h in diag.log.handlers:
            h.flush()
        text = p.read_text(encoding="utf-8")
        assert "UNCAUGHT" in text and "boom-in-thread" in text
    finally:
        _teardown_handlers()
        sys.excepthook, threading.excepthook = saved


# ---- issue-report draft (find_error_excerpt / build_issue_url) ------------------

def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_find_error_excerpt_picks_most_recent_error_with_traceback(tmp_path):
    log_file = _write(tmp_path / "assistkey.log", (
        "2026-08-24 08:00:00 INFO    [MainThread] assistkey: session start\n"
        "2026-08-24 08:00:01 WARNING [MainThread] assistkey.client: reconnect failed; retrying\n"
        "2026-08-24 08:00:02 ERROR   [MainThread] assistkey.client: first failure\n"
        "Traceback (most recent call last):\n"
        "  File \"x.py\", line 1, in <module>\n"
        "ValueError: first\n"
        "2026-08-24 08:00:03 INFO    [MainThread] assistkey: recovered\n"
        "2026-08-24 08:00:04 CRITICAL [MainThread] assistkey: second failure\n"
        "Traceback (most recent call last):\n"
        "  File \"y.py\", line 2, in <module>\n"
        "KeyError: second\n"
    ))
    excerpt = diag.find_error_excerpt([log_file])
    assert "second failure" in excerpt and "KeyError: second" in excerpt
    assert "first failure" not in excerpt          # picked the LATEST block, not the first
    assert "reconnect failed" not in excerpt       # a WARNING must not be mistaken for the block


def test_find_error_excerpt_falls_back_to_tail_when_no_error(tmp_path):
    log_file = _write(tmp_path / "assistkey.log",
                      "2026-08-24 08:00:00 INFO    [MainThread] assistkey: session start\n"
                      "2026-08-24 08:00:01 INFO    [MainThread] assistkey: connected\n")
    excerpt = diag.find_error_excerpt([log_file])
    assert "connected" in excerpt


def test_find_error_excerpt_checks_previous_log_when_current_is_clean(tmp_path):
    current = _write(tmp_path / "assistkey.log",
                     "2026-08-24 09:00:00 INFO    [MainThread] assistkey: session start\n")
    previous = _write(tmp_path / "assistkey.log.1",
                      "2026-08-24 08:00:00 CRITICAL [MainThread] assistkey: UNCAUGHT exception\n"
                      "RuntimeError: boom-previous-run\n")
    excerpt = diag.find_error_excerpt([current, previous])
    assert "boom-previous-run" in excerpt


def test_find_error_excerpt_empty_when_nothing_exists(tmp_path):
    assert diag.find_error_excerpt([tmp_path / "nope.log"]) == ""


def test_find_error_excerpt_truncates_long_blocks():
    huge = "\n".join(f"line {i}" for i in range(2000))
    text = f"2026-08-24 08:00:00 ERROR [MainThread] assistkey: big\n{huge}\n"
    import tempfile
    d = Path(tempfile.mkdtemp())
    log_file = _write(d / "assistkey.log", text)
    excerpt = diag.find_error_excerpt([log_file], max_chars=200)
    assert len(excerpt) < len(text)
    assert "truncated" in excerpt


def test_build_issue_url_points_at_repo_and_has_privacy_warning():
    url = diag.build_issue_url(config=None, log_dir=Path(tempfile.mkdtemp()))
    assert url.startswith(diag.REPO_URL + "/issues/new?")
    body = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["body"][0]
    assert "PUBLIC issue" in body
    assert "(no errors logged" in body   # no log dir content -> the no-error placeholder


def test_build_issue_url_never_leaks_the_token_or_hostname(tmp_path):
    c = cfg.Config(ha_url="https://ha.mypersonaldomain.example:8123", ha_token="SUPER_SECRET_TOKEN_XYZ")
    log_file = _write(tmp_path / "assistkey.log",
                      "2026-08-24 08:00:00 ERROR [MainThread] assistkey: something broke\n")
    url = diag.build_issue_url(config=c, log_dir=tmp_path)
    assert "SUPER_SECRET_TOKEN_XYZ" not in url
    assert "mypersonaldomain" not in url          # the hostname is masked, not just the token
    body = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["body"][0]
    assert "token=set" in body                     # redacted config line present, token value absent
    assert "url=https://(redacted)" in body        # scheme kept (useful), host dropped


def test_build_issue_url_includes_recent_error(tmp_path):
    _write(tmp_path / "assistkey.log",
          "2026-08-24 08:00:00 ERROR [MainThread] assistkey: mic open failed\n"
          "OSError: no such device\n")
    url = diag.build_issue_url(log_dir=tmp_path)
    body = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["body"][0]
    assert "mic open failed" in body and "no such device" in body
