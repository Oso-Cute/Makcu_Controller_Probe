@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if not errorlevel 1 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3 was not found. Install it from https://www.python.org/
    echo During install, check "Add python.exe to PATH".
    pause
    exit /b 1
  )
  set "PY=python"
)

%PY% -c "import serial" >nul 2>&1
if errorlevel 1 (
  echo Installing the required pyserial package...
  %PY% -m pip install pyserial
  if errorlevel 1 (
    echo Could not install pyserial. Try manually:
    echo   %PY% -m pip install pyserial
    pause
    exit /b 1
  )
)

echo Launching Makcu GUI...
%PY% makcu_gui.py
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Makcu GUI exited with error %RC%.
  pause
)
exit /b %RC%
