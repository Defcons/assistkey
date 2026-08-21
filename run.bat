@echo off
REM Debug launcher for Voice PTT (visible console; shows status + errors).
REM For the silent tray-only experience, double-click "Voice PTT.vbs" instead.
cd /d "%~dp0"
".venv\Scripts\python.exe" -u app.py
echo.
echo Voice PTT stopped. Press any key to close.
pause >nul
