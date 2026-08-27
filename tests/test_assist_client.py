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


def test_emit_done_suppressed_once_then_resumes(monkeypatch):
    # request_cancel(suppress_done=True) must swallow exactly the NEXT _emit_done
    # call (the cancelled utterance's own terminal signal), then behave normally
    # again for whatever completes after it.
    import assist_client as ac
    monkeypatch.setattr(ac.sd, "stop", lambda: None)
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client.loop = None
    client.request_cancel(suppress_done=True)
    client._emit_done()
    assert emitted == []                 # suppressed
    client._emit_done()
    assert emitted == [("done",)]         # normal emission resumes after being consumed


def test_plain_request_cancel_does_not_suppress_done(monkeypatch):
    # Tray "Stop" / clicking the popup call request_cancel() with no args — that
    # must NOT suppress the done signal; the popup should dismiss normally.
    import assist_client as ac
    monkeypatch.setattr(ac.sd, "stop", lambda: None)
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client.loop = None
    client.request_cancel()
    client._emit_done()
    assert emitted == [("done",)]


def test_restart_utterance_starts_directly_when_idle():
    # Nothing running -> restart_utterance behaves exactly like start_utterance.
    calls = []
    client = AssistClient(cfg.Config(), ui=lambda _c: None)

    async def fake_start(notify_unavailable=False):
        calls.append(("start", notify_unavailable))
    client.start_utterance = fake_start

    asyncio.run(client.restart_utterance(notify_unavailable=True))
    assert calls == [("start", True)]


def test_restart_utterance_cancels_with_suppress_and_waits_before_starting():
    # Something IS running -> cancel (suppressing its done) and wait for the
    # in-flight utterance to actually wind down before starting the new one.
    order = []
    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    client._active = True
    client._idle.clear()

    def fake_cancel(suppress_done=False):
        order.append(("cancel", suppress_done))

        async def _finish_shortly():
            await asyncio.sleep(0)
            client._idle.set()   # simulate the cancelled utterance winding down
        asyncio.ensure_future(_finish_shortly())
    client.request_cancel = fake_cancel

    async def fake_start(notify_unavailable=False):
        order.append(("start", notify_unavailable))
    client.start_utterance = fake_start

    asyncio.run(client.restart_utterance())
    assert order == [("cancel", True), ("start", False)]


def test_restart_utterance_gives_up_after_timeout_and_still_tries_to_start():
    # Safety net: if the old utterance never winds down, don't hang forever.
    order = []
    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    client._active = True
    client._idle.clear()   # deliberately never set

    def fake_cancel(suppress_done=False):
        order.append(("cancel", suppress_done))
    client.request_cancel = fake_cancel

    async def fake_start(notify_unavailable=False):
        order.append(("start", notify_unavailable))
    client.start_utterance = fake_start

    async def fake_wait_for(coro, timeout):
        coro.close()  # properly discard the awaited coroutine, not just drop it
        raise asyncio.TimeoutError

    async def run_with_faked_timeout():
        import unittest.mock as mock
        with mock.patch("asyncio.wait_for", fake_wait_for):
            await client.restart_utterance()
    asyncio.run(run_with_faked_timeout())
    assert order == [("cancel", True), ("start", False)]


# ---- pump() must reconnect on close, never busy-spin (2026-08-27) ---------------

class _CleanClosedWS:
    """Async-iterates to nothing, exactly like a websockets connection closed with
    a normal 1000 code: __anext__ raises StopAsyncIteration, NOT an exception.
    (Empirically confirmed against real websockets — see ResearchJournal 2026-08-27.)
    This is the case that used to make pump() spin at 100% CPU. The `sleep(0)`
    yields to the loop each pass so that IF the busy-loop regression returns, the
    test's wait_for timeout fires (a fast failure) instead of hanging forever."""
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        raise StopAsyncIteration


class _ErrorClosedWS:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        raise OSError("connection reset")   # OSError is in pump()'s caught tuple


def _pump_until_reconnect(ws):
    """Run the real pump() with a given ws until it calls _reconnect once, then
    stop. Wrapped in a short timeout so a REGRESSION (a busy spin that never
    reaches reconnect) fails fast instead of hanging the suite forever."""
    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    client.ws = ws
    calls = []

    class _Stop(Exception):
        pass

    async def fake_reconnect():
        calls.append(1)
        raise _Stop  # break pump's while-True so the test terminates

    client._reconnect = fake_reconnect

    async def run():
        try:
            await asyncio.wait_for(client.pump(), timeout=2.0)
        except _Stop:
            pass
    asyncio.run(run())
    return calls


def test_pump_reconnects_on_clean_close_instead_of_spinning():
    # THE fix: a clean (no-exception) close must lead to a reconnect. With the bug,
    # pump would re-iterate the dead socket forever and _reconnect is never called
    # (the wait_for timeout would then fire as a TimeoutError, failing the test).
    assert _pump_until_reconnect(_CleanClosedWS()) == [1]


def test_pump_reconnects_on_error_close():
    # The pre-existing error-close path must still reconnect (unchanged behaviour).
    assert _pump_until_reconnect(_ErrorClosedWS()) == [1]
