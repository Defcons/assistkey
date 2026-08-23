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


def test_suspend_ignores_keys_then_resume_restores():
    # While Settings captures a new hotkey the global listener is suspended, so
    # pressing the current hotkey there must not start an utterance.
    hk, log = _make("hold")
    hk.suspend()
    hk._press(Key.f9)
    hk._release(Key.f9)
    assert log == []
    hk.resume()
    hk._press(Key.f9)
    hk._release(Key.f9)
    assert log == ["down", "up"]


def test_canon_to_vks_maps_common_keys():
    from app import _canon_to_vks
    assert _canon_to_vks("k") == {ord("K")}
    assert _canon_to_vks("5") == {ord("5")}
    assert _canon_to_vks("space") == {0x20}
    assert _canon_to_vks("f9") == {0x78}
    assert _canon_to_vks("ctrl") == {0x11, 0xA2, 0xA3}


def test_single_key_hotkey_is_always_suppressed():
    c = cfg.Config(hotkey=["k"], suppress_hotkey=True)
    hk = HotkeyListener(c, on_down=lambda: None, on_up=lambda: None)
    vk = ord("K")
    assert hk._suppress_vk_event(0x0100, vk) is True   # keydown suppressed
    assert hk._suppress_vk_event(0x0101, vk) is True   # keyup suppressed
    assert hk._suppress_vk_event(0x0100, ord("A")) is False  # unrelated key passes through


def test_combo_modifier_passes_through_until_engaged():
    c = cfg.Config(hotkey=["ctrl", "space"], suppress_hotkey=True)
    hk = HotkeyListener(c, on_down=lambda: None, on_up=lambda: None)
    CTRL, SPACE = 0xA2, 0x20
    # Ctrl alone must NOT be suppressed (Ctrl+C etc. still work)
    assert hk._suppress_vk_event(0x0100, CTRL) is False
    # Space while Ctrl held completes the combo -> suppressed
    assert hk._suppress_vk_event(0x0100, SPACE) is True
    # Space by itself (fresh listener) is a normal space
    hk2 = HotkeyListener(c, on_down=lambda: None, on_up=lambda: None)
    assert hk2._suppress_vk_event(0x0100, SPACE) is False


def test_suspended_listener_suppresses_nothing():
    c = cfg.Config(hotkey=["k"], suppress_hotkey=True)
    hk = HotkeyListener(c, on_down=lambda: None, on_up=lambda: None)
    hk.suspend()
    assert hk._suppress_vk_event(0x0100, ord("K")) is False


def test_capture_off_by_default_installs_no_hook_and_suppresses_nothing():
    # Default: no global keyboard hook (that caused system-wide lag), nothing captured.
    c = cfg.Config(hotkey=["k"])   # suppress_hotkey defaults False
    hk = HotkeyListener(c, on_down=lambda: None, on_up=lambda: None)
    assert hk._filter_installed is False
    assert hk._suppress_vk_event(0x0100, ord("K")) is False
