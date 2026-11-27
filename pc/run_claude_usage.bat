@echo off
REM Launches the Claude Zero feed HIDDEN in the system tray (no console window).
REM Right-click the tray icon (bottom-right of the taskbar) -> Quit to stop.
cd /d "%~dp0"
REM First run only: make sure dependencies are installed (brief window).
python -c "import requests, serial, browser_cookie3, pystray, PIL" 1>nul 2>nul || pip install -r "%~dp0..\requirements.txt"
REM pythonw = no console window; a tray icon appears instead.
start "" pythonw "%~dp0claude_usage_backend.py" --tray
