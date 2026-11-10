@echo off
REM One-click start: VERONICA listening for her wake word (voice only).
REM The window only shows status/logs; everything is done by voice.
cd /d "%~dp0"
title VERONICA
REM The Python install manager's own launcher when it's there (same Python), else whatever "python" is.
set "PY=python"
if exist "%LOCALAPPDATA%\Python\bin\python.exe" set "PY=%LOCALAPPDATA%\Python\bin\python.exe"
"%PY%" -m veronica.main --wake
if errorlevel 3 if not errorlevel 4 (
    REM Only one VERONICA listens at a time (she also starts by herself when you sign in).
    echo.
    echo She's already listening - just say "Veronica". This window closes in 10 seconds.
    timeout /t 10 >nul
    exit /b 0
)
if errorlevel 1 (
    echo.
    echo VERONICA stopped with an error - see the messages above or data\veronica.log
    pause
)
