@echo off
REM Build a single-file AssistKey.exe (no Python needed to run it).
REM Requires the dev dependencies:  .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
cd /d "%~dp0"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm AssistKey.spec
echo.
echo Done. The standalone app is dist\AssistKey.exe
pause
