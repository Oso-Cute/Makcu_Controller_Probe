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
    pause
    exit /b 1
  )
  set "PY=python"
)

%PY% -c "import serial, esptool" >nul 2>&1
if errorlevel 1 (
  echo Installing pyserial and esptool...
  %PY% -m pip install pyserial esptool
  if errorlevel 1 (
    echo Could not install the flashing requirements.
    pause
    exit /b 1
  )
)

%PY% flash_probe_firmware.py
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Probe flasher exited with error %RC%.
pause
exit /b %RC%

