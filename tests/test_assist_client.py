"""AssistClient: declining to start an utterance must still resync the app."""
import asyncio

import config as cfg
from assist_client import AssistClient


def test_start_utterance_when_disconnected_emits_done():
    # Regression: a wake trigger (or key-press) can arrive before HA is connected.
    # The app pauses wake-word listening around every utterance and only resumes
    # on ("done",)/("error",). If start_utterance returned silently, wake would
    # stay paused until restart — so it must emit a terminal ("done",).
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    assert client.ws is None
    asyncio.run(client.start_utterance())
    assert ("done",) in emitted


def test_start_utterance_when_active_stays_silent():
    # If an utterance is already running, a second start must NOT emit — the
    # in-flight one owns the terminal ("done",).
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client._active = True
    client.ws = object()  # non-None so we'd proceed if the active-guard were wrong
    asyncio.run(client.start_utterance())
    assert emitted == []


class _FakeState:
    def __init__(self, name):
        self.name = name


class _FakeWS:
    def __init__(self, name):
        self.state = _FakeState(name)


def test_ws_is_open_helper():
    from assist_client import _ws_is_open
    assert _ws_is_open(None) is False
    assert _ws_is_open(_FakeWS("OPEN")) is True
    assert _ws_is_open(_FakeWS("CLOSED")) is False
    assert _ws_is_open(_FakeWS("CONNECTING")) is False


def test_start_utterance_mid_reconnect_notifies_when_interactive():
    # Key pressed during reconnect (socket closed but not None): a deliberate
    # press gets a gentle "Reconnecting…" instead of a raw exception popup.
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client.ws = _FakeWS("CLOSED")
    asyncio.run(client.start_utterance(notify_unavailable=True))
    assert emitted and emitted[0][0] == "error"


def test_start_utterance_mid_reconnect_silent_for_wake():
    # A wake false-positive during reconnect should NOT flash a popup, just resync.
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client.ws = _FakeWS("CLOSED")
    asyncio.run(client.start_utterance())
    assert ("done",) in emitted


def test_is_active_reflects_state():
    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    assert client.is_active() is False
    client._active = True
    assert client.is_active() is True


def test_consume_follow_up_is_one_shot_and_gated():
    client = AssistClient(cfg.Config(follow_up_enabled=True), ui=lambda _c: None)
    client._follow_up_requested = True
    assert client.consume_follow_up() is True
    assert client.consume_follow_up() is False        # consumed
    off = AssistClient(cfg.Config(follow_up_enabled=False), ui=lambda _c: None)
    off._follow_up_requested = True
    assert off.consume_follow_up() is False            # gated by config


def test_request_cancel_safe_without_loop(monkeypatch):
    import assist_client as ac
    called = []
    monkeypatch.setattr(ac.sd, "stop", lambda: called.append(True))
    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    client.loop = None
    client.request_cancel()   # must not raise even with no loop
    assert called == [True]


def test_wants_follow_up_flag_and_question_heuristic():
    from assist_client import AssistClient as AC
    assert AC._wants_follow_up({"continue_conversation": True}, "Done.") is True
    assert AC._wants_follow_up({"response": {"continue_conversation": True}}, "Done.") is True
    assert AC._wants_follow_up({}, "What did you mean?") is True          # trailing question
    assert AC._wants_follow_up({}, "Turned on the lights.") is False       # statement
    assert AC._wants_follow_up({}, "") is False
