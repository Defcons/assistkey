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


def test_restart_utterance_on_timeout_unarms_suppression_and_skips_noop_start():
    # If the old utterance is stuck (e.g. a slow TTS fetch sd.stop can't interrupt),
    # restart must NOT fall through to a start_utterance that would silently no-op
    # while _active is still True, AND must un-arm the suppression it set — otherwise
    # the old run's ("done",) is swallowed and wake stays paused forever (audit
    # 2026-08-27). It cancels, un-arms, and returns; the old run resolves normally.
    order = []
    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    client._active = True
    client._idle.clear()   # deliberately never set -> the wait times out

    def fake_cancel(suppress_done=False):
        order.append(("cancel", suppress_done))
        if suppress_done:
            client._suppress_next_done = True   # mirror the real request_cancel
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
    assert order == [("cancel", True)]           # cancelled, but did NOT no-op-start
    assert client._suppress_next_done is False   # un-armed so the old run's done fires


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


def test_silent_mic_surfaces_error_and_resets_state(monkeypatch):
    # The reported hang: hold-to-talk with the headset OFF. The device stays active and
    # delivers SILENT frames (not zero frames), so it used to fall through to HA and sit
    # in "Thinking…" for seconds before HA's "no text recognized" — and the run then hung
    # in teardown, wedging _active/_idle so every later hotkey press dead-ended. Now a hold
    # whose loudest sample is essentially silence is caught on release -> a clear error AND
    # a clean state reset.
    import assist_client as ac

    class FakeStream:                      # opens fine, delivers digital silence (mic off/muted)
        def __init__(self, *a, callback=None, **k):
            self._cb = callback
        def start(self):
            if self._cb:
                for _ in range(3):
                    self._cb(bytes(ac.BLOCK * 2), ac.BLOCK, None, None)  # BLOCK int16 zeros
        def stop(self): pass
        def close(self): pass
    monkeypatch.setattr(ac.sd, "RawInputStream", FakeStream)
    monkeypatch.setattr(ac.sd, "stop", lambda: None)

    class FakeWS:
        state = _FakeState("OPEN")
        async def send(self, *a): pass

    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client.ws = FakeWS()
    client.active_pipeline_name = lambda: "Assistant"

    async def run():
        client.loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(client.start_utterance())
        await asyncio.sleep(0.05)          # stream opened, event loop entered
        client._release.set()              # user releases the key -> watch_release runs
        await asyncio.wait_for(task, timeout=5)   # must NOT hang; returns promptly
    asyncio.run(run())

    assert any(c[0] == "error" and "microphone" in c[1].lower() for c in emitted), emitted
    assert ("done",) in emitted            # terminal signal fired (resumes wake, resets hotkey)
    assert client._active is False         # state reset -> the next hotkey press works
    assert client._idle.is_set()


def test_pump_disconnect_fails_inflight_routes():
    # 2026-08-29 audit: when the socket drops, HA loses the in-flight run with it —
    # no more events will EVER arrive for that msg_id. pump must fail the live
    # routes immediately, not leave the utterance in "Thinking…" for the 60 s
    # completion watchdog (wake paused the whole time).
    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    client.ws = _CleanClosedWS()
    route: asyncio.Queue = asyncio.Queue()
    client._routes[42] = route

    class _Stop(Exception):
        pass

    async def fake_reconnect():
        raise _Stop

    client._reconnect = fake_reconnect

    async def run():
        try:
            await asyncio.wait_for(client.pump(), timeout=2.0)
        except _Stop:
            pass
    asyncio.run(run())
    assert not route.empty() and route.get_nowait() == {"type": "__disconnected__"}


class _OpenDyingWS:
    """Passes the _ws_is_open check, then raises on the first send — the socket
    dying exactly between start_utterance's open-check and the pipeline-run send."""
    state = _FakeState("OPEN")

    async def send(self, *a):
        raise OSError("socket died at press time")


class _RecordingStream:
    """Opens fine, delivers nothing; records whether anyone ever stopped it."""
    last = None

    def __init__(self, *a, **k):
        self.stopped = self.closed = False
        _RecordingStream.last = self

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def test_setup_send_failure_closes_stream_and_route(monkeypatch):
    # 2026-08-29 audit (probe-confirmed): if the run-send raises, forward_audio —
    # the only stream closer — was never created. Without explicit cleanup the mic
    # stayed HOT (still capturing) forever and the routes entry leaked.
    import assist_client as ac
    monkeypatch.setattr(ac.sd, "RawInputStream", _RecordingStream)
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client.ws = _OpenDyingWS()
    client.active_pipeline_name = lambda: "Assistant"

    async def run():
        client.loop = asyncio.get_running_loop()
        await client.start_utterance()
    asyncio.run(run())

    s = _RecordingStream.last
    assert s.stopped and s.closed, "mic stream must be closed on a setup-send failure"
    assert client._routes == {}, "routes entry must not leak"
    assert any(c[0] == "error" for c in emitted)   # terminal signal still fires
    assert client._active is False and client._idle.is_set()


class _OpenQuietWS:
    """OPEN socket that accepts sends silently (events are injected by the test)."""
    state = _FakeState("OPEN")

    async def send(self, *a):
        pass


def _run_utterance_with_injected_event(monkeypatch, arrange):
    """Start a real utterance on a quiet fake socket, then let `arrange(client, q)`
    inject events into its route queue; return the emitted ui commands."""
    import assist_client as ac
    monkeypatch.setattr(ac.sd, "RawInputStream", _RecordingStream)
    emitted = []
    client = AssistClient(cfg.Config(), ui=emitted.append)
    client.ws = _OpenQuietWS()
    client.active_pipeline_name = lambda: "Assistant"

    async def run():
        client.loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(client.start_utterance())
        await asyncio.sleep(0.05)                     # utterance is at events.get()
        q = next(iter(client._routes.values()))
        arrange(client, q)
        await asyncio.wait_for(task, timeout=5)
    asyncio.run(run())
    return emitted, client


def test_disconnected_sentinel_fails_run_with_clear_error(monkeypatch):
    emitted, _ = _run_utterance_with_injected_event(
        monkeypatch, lambda c, q: q.put_nowait({"type": "__disconnected__"}))
    assert any(c[0] == "error" and "connection" in c[1].lower() for c in emitted)
    assert ("done",) in emitted


def test_crash_error_is_suppressed_for_cancelled_run(monkeypatch):
    # 2026-08-29 audit: the error channel used to be un-suppressible — a crashing
    # OLD run (bad HA event shape) could emit ("error",) after a barge-in already
    # started the NEW run, resetting hotkey/wake state mid-gesture (the same race
    # the done-channel suppression closed). A cancelled run must crash silently.
    def arrange(client, q):
        client._cancel.set()                                # barge-in already happened
        q.put_nowait({"type": "event",
                      "event": {"type": "run-start", "data": {}}})  # KeyError: runner_data
    emitted, client = _run_utterance_with_injected_event(monkeypatch, arrange)
    assert not any(c[0] == "error" for c in emitted), emitted
    assert ("done",) in emitted                             # terminal still fires
    assert client._follow_up_requested is False


def test_crash_error_precedes_done_for_live_run(monkeypatch):
    # Same crash WITHOUT a cancel: the error must surface, and BEFORE the done
    # (correct terminal ordering for the app's state resets).
    emitted, _ = _run_utterance_with_injected_event(
        monkeypatch, lambda c, q: q.put_nowait(
            {"type": "event", "event": {"type": "run-start", "data": {}}}))
    kinds = [c[0] for c in emitted]
    assert "error" in kinds and ("done",) in emitted
    assert kinds.index("error") < kinds.index("done")


def test_request_once_routes_inflight_frames():
    # 2026-08-29 audit: right after a reconnect, _request_once (load_pipelines)
    # reads the socket while a fresh utterance may already be running — frames for
    # other ids must be routed to them, not silently discarded.
    import json as _json

    class _FeedWS:
        state = _FakeState("OPEN")

        def __init__(self, frames):
            self._frames = list(frames)

        async def send(self, *a):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._frames:
                raise StopAsyncIteration
            return self._frames.pop(0)

    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    route: asyncio.Queue = asyncio.Queue()
    client._routes[7] = route
    foreign = {"id": 7, "type": "event", "event": {"type": "run-start"}}

    async def run():
        client.ws = _FeedWS([_json.dumps(foreign),
                             _json.dumps({"id": client._next_id + 1, "success": True})])
        return await client._request_once({"type": "x"})
    res = asyncio.run(run())
    assert res.get("success") is True
    assert route.get_nowait() == foreign


def test_reconnect_backoff_interruptible_by_kick():
    # 2026-08-29 audit: a Settings save with corrected credentials must cut the
    # backoff sleep short instead of leaving the user staring at "Disconnected"
    # for up to 30 s after fixing a typo.
    import time as _time
    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    attempts = []

    async def fake_connect():
        attempts.append(_time.monotonic())
        if len(attempts) < 2:
            raise OSError("still down")

    async def fake_load():
        pass
    client.connect = fake_connect
    client.load_pipelines = fake_load

    async def run():
        task = asyncio.ensure_future(client._reconnect())
        await asyncio.sleep(0.05)          # first attempt failed; now in its 1 s backoff
        client._retry_kick.set()           # what force_reconnect does on a creds change
        await asyncio.wait_for(task, timeout=2)
    t0 = _time.monotonic()
    asyncio.run(run())
    assert len(attempts) == 2
    assert _time.monotonic() - t0 < 0.6, "kick must cut the 1 s backoff short"


def test_play_returns_promptly_on_barge_in_during_fetch(monkeypatch):
    # The TTS fetch used to be uninterruptible (urlopen in an executor), so a
    # barge-in during a slow fetch left the utterance stuck at `await self._play`
    # for up to the whole timeout. Now a barge-in returns _play immediately; the
    # fetch finishes in the background and skips playback.
    import threading
    import assist_client as ac

    gate = threading.Event()

    class FakeResp:
        def read(self): return b"x"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(ac.urllib.request, "urlopen",
                        lambda req, timeout=None: (gate.wait(), FakeResp())[1])
    monkeypatch.setattr(ac.sd, "stop", lambda: None)

    client = AssistClient(cfg.Config(), ui=lambda _c: None)
    client.server, client.token = "http://x", "t"

    async def run():
        client.loop = asyncio.get_running_loop()
        client._cancel = asyncio.Event()
        play = asyncio.ensure_future(client._play({"url": "http://x/t.mp3"}))
        await asyncio.sleep(0.1)              # _play now blocked in the (fake) fetch
        assert not play.done()
        t0 = asyncio.get_running_loop().time()
        client.request_cancel()              # barge-in DURING the fetch
        await asyncio.wait_for(play, timeout=1.0)   # must return without waiting on the fetch
        dt = asyncio.get_running_loop().time() - t0
        gate.set()                           # release the orphan fetch before the loop closes
        await asyncio.sleep(0.05)
        return dt

    dt = asyncio.run(run())
    assert dt < 0.5, f"_play took {dt:.2f}s to return on barge-in (should be near-instant)"
