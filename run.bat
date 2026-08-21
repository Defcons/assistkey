@echo off
REM Debug launcher for AssistKey (visible console; shows status + errors).
REM For the silent tray-only experience, double-click "AssistKey.vbs" instead.
cd /d "%~dp0"
".venv\Scripts\python.exe" -u app.py
echo.
echo AssistKey stopped. Press any key to close.
pause >nul
