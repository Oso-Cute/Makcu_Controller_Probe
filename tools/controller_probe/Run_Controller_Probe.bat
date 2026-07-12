@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

where py >nul 2>&1
if not errorlevel 1 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3 was not found. Install it from https://www.python.org/
    pause
    exit /b 1
  )
  set "PY=python"
)

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo Controller Probe requires Python 3.10 or newer.
  pause
  exit /b 1
)

%PY% -c "import serial" >nul 2>&1
if errorlevel 1 (
  echo Installing the required pyserial package...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Could not install pyserial.
    pause
    exit /b 1
  )
)

%PY% controller_probe.py
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Controller Probe exited with error %RC%.
pause
exit /b %RC%
