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


def test_build_issue_url_points_at_the_repo():
    assert diag.build_issue_url().startswith(diag.REPO_URL + "/issues/new?")


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# ---- find_error_excerpt: which raw text gets pulled in --------------------------

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
    assert "connected" in diag.find_error_excerpt([log_file])


def test_find_error_excerpt_checks_previous_log_when_current_is_clean(tmp_path):
    current = _write(tmp_path / "assistkey.log",
                     "2026-08-24 09:00:00 INFO    [MainThread] assistkey: session start\n")
    previous = _write(tmp_path / "assistkey.log.1",
                      "2026-08-24 08:00:00 CRITICAL [MainThread] assistkey: UNCAUGHT exception\n"
                      "RuntimeError: boom-previous-run\n")
    assert "boom-previous-run" in diag.find_error_excerpt([current, previous])


def test_find_error_excerpt_empty_when_nothing_exists(tmp_path):
    assert diag.find_error_excerpt([tmp_path / "nope.log"]) == ""


def test_find_error_excerpt_truncates_long_blocks(tmp_path):
    huge = "\n".join(f"line {i}" for i in range(2000))
    log_file = _write(tmp_path / "assistkey.log",
                      f"2026-08-24 08:00:00 ERROR [MainThread] assistkey: big\n{huge}\n")
    excerpt = diag.find_error_excerpt([log_file], max_chars=200)
    assert len(excerpt) < len(huge)
    assert "truncated" in excerpt


# ---- _redact: privacy data stripped, everything else kept -----------------------

def test_redact_strips_the_configured_ha_url_and_host():
    c = cfg.Config(ha_url="https://ha.mypersonaldomain.example:8123")
    text = "ConnectionError: could not reach https://ha.mypersonaldomain.example:8123/api"
    out = diag._redact(text, c)
    assert "mypersonaldomain" not in out
    assert "[home-assistant-url]" in out


def test_redact_strips_urls_even_without_config():
    out = diag._redact("failed to fetch https://models.example.org/wake/model.onnx", config=None)
    assert "models.example.org" not in out
    assert "[url]" in out


def test_redact_strips_jwt_shaped_tokens():
    fake_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    out = diag._redact(f"auth failed with token {fake_token}")
    assert fake_token not in out
    assert "[token]" in out


def test_redact_strips_windows_username_but_keeps_the_rest_of_the_path():
    out = diag._redact('File "C:\\Users\\David\\assistkey\\assist_client.py", line 228')
    assert "David" not in out
    assert "C:\\Users\\[user]\\assistkey\\assist_client.py" in out


def test_redact_strips_ipv4_addresses():
    out = diag._redact("Failed to establish a connection to 192.168.1.50:8123")
    assert "192.168.1.50" not in out
    assert "[ip]" in out


def test_redact_keeps_non_sensitive_error_content():
    out = diag._redact("OSError: no such audio device")
    assert out == "OSError: no such audio device"   # nothing to redact -> unchanged


# ---- build_issue_url: end-to-end, using real Config + a realistic log -----------

def test_build_issue_url_redacts_url_path_and_ip_but_keeps_the_error(tmp_path):
    c = cfg.Config(ha_url="https://ha.mypersonaldomain.example:8123", ha_token="realtoken123")
    _write(tmp_path / "assistkey.log", (
        "2026-08-24 08:00:00 ERROR [MainThread] assistkey.client: mic open failed\n"
        "Traceback (most recent call last):\n"
        "  File \"C:\\Users\\David\\assistkey\\assist_client.py\", line 228, in _run_utterance\n"
        "ConnectionError: could not reach https://ha.mypersonaldomain.example:8123 "
        "(also tried 192.168.1.50) — PortAudioError: Invalid device\n"
    ))
    url = diag.build_issue_url(config=c, log_dir=tmp_path)
    assert "mypersonaldomain" not in url and "David" not in url and "192.168.1.50" not in url
    assert "realtoken123" not in url
    body = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["body"][0]
    assert "mic open failed" in body and "PortAudioError: Invalid device" in body  # real signal kept
    assert "[home-assistant-url]" in body and "[user]" in body and "[ip]" in body


def test_build_issue_url_placeholder_when_no_errors_logged(tmp_path):
    url = diag.build_issue_url(log_dir=tmp_path)
    body = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["body"][0]
    assert "no errors logged" in body
