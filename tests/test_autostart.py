"""Autostart command builder (no registry writes in tests)."""
import sys

import autostart


def test_launch_command_points_at_vbs():
    cmd = autostart._launch_command()
    assert "wscript.exe" in cmd.lower()
    assert cmd.rstrip().endswith('AssistKey.vbs"')


def test_launch_command_frozen_points_at_exe(monkeypatch, tmp_path):
    # A frozen build has no VBS beside a downloaded exe — launch the exe directly.
    exe = tmp_path / "AssistKey.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    cmd = autostart._launch_command()
    assert "wscript" not in cmd.lower()
    assert cmd.strip().strip('"').lower().endswith("assistkey.exe")
