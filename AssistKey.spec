# PyInstaller spec — build a single-file, windowed (no console) tray exe:
#   .venv\Scripts\python.exe -m PyInstaller --noconfirm AssistKey.spec
# Output: dist\AssistKey.exe  (no Python install needed to run it)
#
# openWakeWord models are NOT bundled — they download to a per-user cache on first
# enable, exactly as in a source run.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [("icon.ico", ".")]
datas += collect_data_files("customtkinter")   # themes/assets ctk loads at runtime
datas += collect_data_files("openwakeword")     # any base models shipped in the wheel

hiddenimports = collect_submodules("openwakeword")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AssistKey",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed: no console window (tray app)
    icon="icon.ico",
)
