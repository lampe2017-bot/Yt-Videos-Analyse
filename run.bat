@echo off
REM YouTube Cockpit Runner. Doppelklicken oder im Task Scheduler verwenden.
cd /d "%~dp0"
python cockpit.py %*
if errorlevel 1 (
    echo.
    echo Pipeline mit Fehler beendet.
    pause
    exit /b 1
)
echo.
echo Dashboard: %~dp0output\cockpit.html
start "" "%~dp0output\cockpit.html"
