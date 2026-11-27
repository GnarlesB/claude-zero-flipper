@echo off
REM Run once: creates a "Claude Zero" button on your Desktop with the icon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
echo.
echo If you see "DONE - created" above, the button is on your desktop.
pause
