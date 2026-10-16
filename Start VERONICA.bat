@echo off
REM One-click start: VERONICA listening for her wake word (voice only).
REM The window only shows status/logs; everything is done by voice.
cd /d "%~dp0"
title VERONICA
python -m veronica.main --wake
if errorlevel 1 (
    echo.
    echo VERONICA stopped with an error - see the messages above or data\veronica.log
    pause
)
