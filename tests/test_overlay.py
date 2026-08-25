"""Overlay: the popup must never get stuck — dismissal + failure recovery."""
import time

import tkinter as tk
import pytest

import config as cfg
from overlay import Overlay, HIDDEN


def _pump(root, seconds):
    """Drive the Tk event loop (processes `after` timers) for a while."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


@pytest.fixture(scope="module")
def root():
    # One shared root for the module — re-creating Tk() per test is flaky.
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


def test_hard_hide_resets_everything(root):
    ov = Overlay(root, cfg.Config())
    ov.listening()
    _pump(root, 0.3)
    ov._hard_hide()
    assert ov._state == HIDDEN
    assert ov._shown is False
    assert ov._animating is False


def test_done_dismisses_popup(root):
    c = cfg.Config()
    c.dismiss_seconds = 0.2
    ov = Overlay(root, c)
    ov.listening()
    _pump(root, 0.2)
    ov.thinking()
    ov.set_user_text("hello world")
    _pump(root, 0.15)
    ov.response_reset()
    ov.response_append("hi there")
    _pump(root, 0.2)
    ov.done()
    _pump(root, 1.2)  # dismiss_seconds + slide-out + hard-hide backstop
    assert ov._shown is False


def test_frame_exception_does_not_wedge_animating(root):
    ov = Overlay(root, cfg.Config())

    def boom(_y):
        raise RuntimeError("frame error")

    ov._place = boom  # every animation frame now throws
    ov.listening()
    _pump(root, 0.4)
    # A frame error must still complete the transition, not leave it wedged.
    assert ov._animating is False


def test_watchdog_forces_stuck_terminal_popup_away(root):
    c = cfg.Config()
    c.dismiss_seconds = 1
    ov = Overlay(root, c)
    ov.response_reset()
    ov.response_append("stuck reply")
    _pump(root, 0.3)
    # Simulate a wedge: it's shown, terminal, and the dismiss never ran.
    ov._cancel_dismiss()
    ov._last_change -= 60  # pretend nothing has changed for a long time
    _pump(root, 3.2)  # watchdog ticks every 3s
    assert ov._shown is False


def test_set_level_clamps(root):
    ov = Overlay(root, cfg.Config())
    ov.set_level(2.0)
    assert ov._level == 1.0            # clamped high
    ov.set_level(-5.0)
    assert 0.0 <= ov._level <= 1.0     # clamped low (with release smoothing)


def test_transcript_stays_visible_during_response(root):
    # The user's recognised words should persist into the RESPONSE popup
    # (not just Thinking) so they can confirm they were understood correctly
    # for as long as the reply is shown.
    ov = Overlay(root, cfg.Config())
    ov.listening()
    _pump(root, 0.6)  # let the slide-in fully settle before the next transition
    ov.thinking()
    _pump(root, 0.6)  # ditto for listening -> thinking, or response_reset below
    ov.set_user_text("turn on the office lights")  # its own state, no transition
    _pump(root, 0.2)
    ov.response_reset()
    _pump(root, 0.6)  # let thinking -> response fully settle
    ov.response_append("Sure, turning them on.")
    _pump(root, 0.3)
    texts = [ov.canvas.itemcget(i, "text") for i in ov.canvas.find_all()
             if ov.canvas.type(i) == "text"]
    assert any("turn on the office lights" in t for t in texts)
    assert any("turning them on" in t for t in texts)


def test_transcript_resets_on_new_utterance(root):
    ov = Overlay(root, cfg.Config())
    ov.listening()
    ov.thinking()
    ov.set_user_text("some old command")
    _pump(root, 0.3)
    ov.listening()  # a fresh utterance starting
    assert ov._user_text == ""
