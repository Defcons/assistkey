"""Persistent settings for the AssistKey tray app.

Stored as config.json next to this file: the hotkey (a set of keys that must all
be held), trigger mode, mic / speaker device indices, the chosen HA Assist
pipeline, wake-word options and popup preferences — plus the Home Assistant URL
and long-lived token. `credentials()` resolves the URL/token from config first,
falling back to the HASS_SERVER / HASS_TOKEN environment variables. Because the
token is written here, config.json is sensitive and git-ignored.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from pynput import keyboard as kb

import dpapi

CONFIG_PATH = Path(__file__).with_name("config.json")

# Normalise left/right modifier variants to one canonical token so a combo
# matches whichever physical key is pressed.
_MOD_MAP = {
    "ctrl_l": "ctrl", "ctrl_r": "ctrl", "ctrl": "ctrl",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "alt": "alt",
    "shift_l": "shift", "shift_r": "shift", "shift": "shift",
    "cmd_l": "cmd", "cmd_r": "cmd", "cmd": "cmd",
}


def key_to_canon(key) -> str:
    """Map a pynput key to a stable string token used for storage + matching."""
    if isinstance(key, kb.Key):
        return _MOD_MAP.get(key.name, key.name)
    # KeyCode (letters, digits, punctuation)
    if getattr(key, "char", None):
        return key.char.lower()
    if getattr(key, "vk", None) is not None:
        return f"vk{key.vk}"
    return str(key)


def hotkey_label(tokens) -> str:
    """Human-friendly name, e.g. ['ctrl','space'] -> 'Ctrl + Space'."""
    order = {"ctrl": 0, "alt": 1, "shift": 2, "cmd": 3}
    pretty = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win",
              "space": "Space", "esc": "Esc"}
    parts = sorted(tokens, key=lambda t: order.get(t, 99))
    return " + ".join(pretty.get(t, t.upper() if len(t) <= 3 else t.capitalize())
                      for t in parts) or "(none)"


@dataclass
class Config:
    ha_url: str = ""                       # e.g. https://homeassistant.local:8123
    ha_token: str = ""                     # HA long-lived access token
    hotkey: list[str] = field(default_factory=lambda: ["f9"])
    trigger_mode: str = "hold"             # "hold" (press-and-hold) or "toggle" (tap on/off)
    wake_enabled: bool = False             # listen for a wake word (openWakeWord)
    wake_word: str = "hey_jarvis"          # openWakeWord model name
    wake_sensitivity: float = 0.5          # 0–1 detection threshold (higher = stricter)
    mic_device: int | None = None          # sounddevice index; None = default
    speaker_device: int | None = None      # sounddevice index; None = default
    pipeline: str | None = None            # HA pipeline id; None = preferred
    dismiss_seconds: float = 2.0           # seconds the popup lingers AFTER the reply is spoken
    popup_monitor: str = "primary"         # "primary" | "cursor" | monitor index ("0", "1", …)
    follow_up_enabled: bool = False        # auto-listen for a follow-up when HA asks a question

    @property
    def hotkey_set(self) -> frozenset[str]:
        return frozenset(self.hotkey)

    def credentials(self) -> tuple[str, str]:
        """Resolve (url, token): config values first, else HASS_SERVER/HASS_TOKEN env."""
        url = (self.ha_url or "").strip() or (os.environ.get("HASS_SERVER") or "").strip()
        token = (self.ha_token or "").strip() or (os.environ.get("HASS_TOKEN") or "").strip()
        return url, token

    def is_configured(self) -> bool:
        url, token = self.credentials()
        return bool(url and token)

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                known = {f for f in cls.__dataclass_fields__}
                obj = cls(**{k: v for k, v in data.items() if k in known})
                obj.ha_token = dpapi.unprotect(obj.ha_token)  # decrypt at rest -> plaintext in memory
                return obj
            except Exception as exc:  # noqa: BLE001 - corrupt config shouldn't crash startup
                print(f"config.json unreadable ({exc}); using defaults")
        return cls()

    def save(self) -> None:
        data = {
            "ha_url": self.ha_url,
            "ha_token": dpapi.protect(self.ha_token),  # encrypted at rest (DPAPI, per-user)
            "hotkey": self.hotkey,
            "trigger_mode": self.trigger_mode,
            "wake_enabled": self.wake_enabled,
            "wake_word": self.wake_word,
            "wake_sensitivity": self.wake_sensitivity,
            "mic_device": self.mic_device,
            "speaker_device": self.speaker_device,
            "pipeline": self.pipeline,
            "dismiss_seconds": self.dismiss_seconds,
            "popup_monitor": self.popup_monitor,
            "follow_up_enabled": self.follow_up_enabled,
        }
        # Atomic write: a truncating write interrupted mid-flight (this app
        # force-kills older instances at startup) would corrupt config.json and
        # lose the token. Write a temp file, then rename over the target.
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
