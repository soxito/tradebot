@echo off
REM ==========================================================================
REM  TradeBot Windows launcher
REM  run-local.sh is bash-only; on Windows use this wrapper around start.py,
REM  which is fully cross-platform (auto-installs Node/Python/VC++ via winget,
REM  uses Docker for Postgres/Redis, and tunes itself to the machine's specs).
REM
REM    start.bat            Start everything (auto-detects DB mode -> Docker)
REM    start.bat --docker   Force Docker Postgres/Redis
REM    start.bat --stop     Stop all services
REM    start.bat --status   Show what's running
REM ==========================================================================
setlocal

REM Prefer the Python launcher (py) with 3.13/3.12/3.11, then fall back to python.
where py >nul 2>&1
if %errorlevel%==0 (
  py -3.13 "%~dp0start.py" %* 2>nul || py -3.12 "%~dp0start.py" %* 2>nul || py -3.11 "%~dp0start.py" %* 2>nul || py -3 "%~dp0start.py" %*
) else (
  python "%~dp0start.py" %*
)

endlocal
