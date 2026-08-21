"""HotkeyListener: hold vs toggle semantics, auto-repeat guard, resync."""
from pynput.keyboard import Key

import config as cfg
from app import HotkeyListener


def _make(mode):
    c = cfg.Config(hotkey=["f9"], trigger_mode=mode)
    log = []
    hk = HotkeyListener(c, on_down=lambda: log.append("down"), on_up=lambda: log.append("up"))
    return hk, log


def test_hold_basic():
    hk, log = _make("hold")
    hk._press(Key.f9)
    hk._press(Key.f9)   # auto-repeat while held — must not re-fire
    hk._release(Key.f9)
    assert log == ["down", "up"]


def test_hold_two_cycles():
    hk, log = _make("hold")
    for _ in range(2):
        hk._press(Key.f9)
        hk._release(Key.f9)
    assert log == ["down", "up", "down", "up"]


def test_toggle_on_off_on():
    hk, log = _make("toggle")
    for _ in range(3):
        hk._press(Key.f9)
        hk._release(Key.f9)
    assert log == ["down", "up", "down"]


def test_toggle_ignores_autorepeat():
    hk, log = _make("toggle")
    hk._press(Key.f9)
    hk._press(Key.f9)
    hk._press(Key.f9)
    hk._release(Key.f9)
    assert log == ["down"]


def test_toggle_mark_idle_resyncs():
    hk, log = _make("toggle")
    hk._press(Key.f9)
    hk._release(Key.f9)      # down
    hk.mark_idle()           # utterance ended on its own
    hk._press(Key.f9)
    hk._release(Key.f9)      # should be down again, not up
    assert log == ["down", "down"]
