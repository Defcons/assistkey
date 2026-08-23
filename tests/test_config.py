"""Config: key serialization, credential resolution, save/load."""
from pynput.keyboard import Key, KeyCode

import config as cfg


def test_key_to_canon_normalizes_modifiers():
    assert cfg.key_to_canon(Key.f9) == "f9"
    assert cfg.key_to_canon(Key.ctrl_l) == "ctrl"
    assert cfg.key_to_canon(Key.ctrl_r) == "ctrl"
    assert cfg.key_to_canon(Key.space) == "space"
    assert cfg.key_to_canon(KeyCode(char="A")) == "a"


def test_hotkey_label():
    assert cfg.hotkey_label(["f9"]) == "F9"
    assert cfg.hotkey_label(["ctrl", "space"]) == "Ctrl + Space"


def test_credentials_prefers_config_over_env(monkeypatch):
    monkeypatch.setenv("HASS_SERVER", "http://env:8123")
    monkeypatch.setenv("HASS_TOKEN", "envtoken")
    c = cfg.Config(ha_url="http://cfg:8123", ha_token="cfgtoken")
    assert c.credentials() == ("http://cfg:8123", "cfgtoken")
    assert c.is_configured()


def test_credentials_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("HASS_SERVER", "http://env:8123")
    monkeypatch.setenv("HASS_TOKEN", "envtoken")
    assert cfg.Config().credentials() == ("http://env:8123", "envtoken")


def test_not_configured_when_nothing_set(monkeypatch):
    monkeypatch.delenv("HASS_SERVER", raising=False)
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    assert not cfg.Config().is_configured()


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
    c = cfg.Config(ha_url="http://x:8123", ha_token="tok", hotkey=["ctrl", "space"],
                   trigger_mode="toggle", dismiss_seconds=8.0)
    c.save()
    loaded = cfg.Config.load()
    assert loaded.ha_url == "http://x:8123"
    assert loaded.hotkey == ["ctrl", "space"]
    assert loaded.trigger_mode == "toggle"
    assert loaded.dismiss_seconds == 8.0


def test_token_encrypted_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
    cfg.Config(ha_token="supersecret").save()
    raw = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "supersecret" not in raw          # plaintext must never hit disk
    assert '"dpapi:' in raw                   # stored as a DPAPI blob
    assert cfg.Config.load().ha_token == "supersecret"   # decrypts back in memory
