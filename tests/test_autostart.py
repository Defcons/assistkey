"""Autostart command builder (no registry writes in tests)."""
import autostart


def test_launch_command_points_at_vbs():
    cmd = autostart._launch_command()
    assert "wscript.exe" in cmd.lower()
    assert cmd.rstrip().endswith('AssistKey.vbs"')
