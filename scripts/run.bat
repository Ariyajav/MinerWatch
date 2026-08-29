@echo off
REM Start MinerWatch from a plain cmd.exe session.
REM
REM Task Scheduler entries and double-clicks start in C:\Windows\System32, so
REM the working directory is set explicitly. PYTHONUTF8 keeps non-ASCII log
REM output from raising UnicodeEncodeError on a legacy console code page.
REM
REM Everything is rehearsed unless you pass --live / --live-sleep /
REM --live-watchdog, e.g.:  run.bat --live-sleep

setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
    echo No virtual environment found. Run scripts\setup.ps1 first.
    exit /b 1
)

".venv\Scripts\python.exe" -m minerwatch -c miners.yaml run %*
exit /b %ERRORLEVEL%
