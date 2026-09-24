@echo off
rem One-step launcher for Windows: double-click this file.
rem Creates a private Python environment in .venv the first time,
rem keeps its packages up to date, and opens the web app in your browser.
setlocal
cd /d "%~dp0\.."

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating a Python environment in .venv ^(this takes a minute^)...
  %PY% -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
)
".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :error
".venv\Scripts\python.exe" -m lucidfish web %*
goto :eof

:error
echo.
echo Setup failed - see the messages above. The README's Troubleshooting section can help.
pause
exit /b 1
