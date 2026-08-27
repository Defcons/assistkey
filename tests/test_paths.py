"""Frozen-aware app_dir(): user files live next to the app, never in _MEIPASS."""
import sys
from pathlib import Path

import config
import diag
import paths


def test_app_dir_source_is_the_source_tree():
    # Not frozen (a pytest run) -> the directory that holds paths.py.
    assert paths.app_dir() == Path(paths.__file__).resolve().parent


def test_app_dir_frozen_is_the_exe_folder(monkeypatch, tmp_path):
    exe = tmp_path / "AssistKey.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    # Frozen build -> the folder CONTAINING the exe, not sys._MEIPASS.
    assert paths.app_dir() == tmp_path.resolve()


def test_config_and_log_paths_hang_off_app_dir():
    # The two user-writable files must resolve under app_dir(), or a frozen build
    # would read/write them in the temp extraction dir and lose them on restart.
    assert config.CONFIG_PATH == paths.app_dir() / "config.json"
    assert diag.LOG_PATH == paths.app_dir() / "assistkey.log"
