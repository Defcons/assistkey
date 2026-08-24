"""Diagnostics logging: token redaction, stray-output capture, file output, crash capture."""
import logging
import sys
import threading
import types
import urllib.parse

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

def test_build_issue_url_points_at_the_repo():
    url = diag.build_issue_url()
    assert url.startswith(diag.REPO_URL + "/issues/new?")


def test_build_issue_url_takes_no_arguments_and_touches_no_files():
    # The draft must not depend on (or read) any log file or config at all.
    import inspect
    assert list(inspect.signature(diag.build_issue_url).parameters) == ["repo_url"]


def test_build_issue_url_body_has_no_log_content_or_config_values():
    url = diag.build_issue_url()
    body = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["body"][0]
    # Nothing user/machine-specific: no config keys, no URL scheme, no "log excerpt".
    for leak in ("url=", "token=", "hotkey=", "mic=", "spk=", "http://", "https://", "excerpt"):
        assert leak not in body, f"unexpected {leak!r} in an anonymous issue draft"
    assert "System" in body and "Python" in body and "OS" in body   # generic env info stays


def test_build_issue_url_never_contains_a_token_even_if_one_exists_in_the_process(monkeypatch):
    # Defence in depth: even if something set an env var with a token-shaped value,
    # the anonymous template has no path for it to reach the URL.
    monkeypatch.setenv("HASS_TOKEN", "SUPER_SECRET_TOKEN_XYZ")
    url = diag.build_issue_url()
    assert "SUPER_SECRET_TOKEN_XYZ" not in url
