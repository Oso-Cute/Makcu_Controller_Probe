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

set "FLASHER=%~dp0flash_probe_firmware.py"

rem In the shareable package the flasher and merged images are beside this
rem batch file.  In the source workbench, assemble that package from the
rem existing LEFT_PROBE and RIGHT_PROBE build outputs first.
if not exist "%FLASHER%" (
  echo Preparing the probe firmware package from the current probe builds...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_package.ps1" -SkipBuild
  if errorlevel 1 (
    echo.
    echo Could not prepare the probe firmware package.
    echo Build LEFT_PROBE and RIGHT_PROBE, then try again.
    pause
    exit /b 1
  )
  rem Pick the newest packaged version; the loop keeps the last match.
  for /f "delims=" %%D in ('dir /b /ad /o:n "%~dp0..\..\dist\MAKCU_Controller_Probe_v*" 2^>nul') do set "FLASHER=%~dp0..\..\dist\%%D\flash_probe_firmware.py"
)

if not exist "%FLASHER%" (
  echo Probe flasher was not found after packaging:
  echo   %FLASHER%
  pause
  exit /b 1
)

%PY% "%FLASHER%"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Probe flasher exited with error %RC%.
pause
exit /b %RC%
