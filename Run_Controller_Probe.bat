@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

rem ---- Fast path -----------------------------------------------------------
rem A previous run that exited cleanly leaves venv\.setup_ok. When both it and
rem the venv interpreter exist, skip all setup and launch immediately. Delete
rem the venv folder (or just venv\.setup_ok) to force a fresh setup.
if exist "venv\Scripts\python.exe" if exist "venv\.setup_ok" (
    set "VPY=venv\Scripts\python.exe"
    echo Environment ready. Starting the Controller Probe...
    echo.
    goto :launch
)

echo ============================================
echo    MAKCU Controller Probe - Setup and Run
echo ============================================
echo.

rem ---- [1/3] Find a usable Python (3.10+) ----------------------------------
echo [1/3] Checking Python installation...
set "PYCMD="
call :try_python py -3
if defined PYCMD goto :python_ready
call :try_python python
if defined PYCMD goto :python_ready
call :try_python python3
if defined PYCMD goto :python_ready

rem None found (or too old): offer to install it automatically via winget.
echo Python 3.10+ was not found on this PC.
where winget >nul 2>&1
if errorlevel 1 goto :no_python_manual

echo Installing Python 3.12 for you (no admin needed). One-time download...
echo.
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
echo.
echo Re-checking for Python...
call :try_python py -3
if defined PYCMD goto :python_ready
call :try_python python
if defined PYCMD goto :python_ready

rem Installed but this window's PATH is stale: ask for one restart.
echo.
echo Python was installed, but this window needs to reopen to see it.
echo Please CLOSE this window and double-click Run_Controller_Probe.bat again.
echo.
pause
exit /b 0

:no_python_manual
echo.
echo Could not install Python automatically.
echo A download page will open. Install Python, tick "Add python.exe to PATH",
echo then run this file again.
echo.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:python_ready
%PYCMD% --version
echo Python is ready.
echo.

rem ---- [2/3] Create/keep a local virtual environment -----------------------
echo [2/3] Preparing a local environment (venv)...
if not exist "venv\Scripts\python.exe" (
    echo Creating the environment, one moment...
    %PYCMD% -m venv venv
    if errorlevel 1 (
        echo.
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
    echo Environment created.
) else (
    echo Environment already set up.
)
set "VPY=venv\Scripts\python.exe"
echo.

rem ---- [3/3] Install the pyserial requirement into the venv ----------------
echo [3/3] Installing requirements...
"%VPY%" -c "import serial" >nul 2>&1
if errorlevel 1 (
    "%VPY%" -m pip install --disable-pip-version-check --quiet --upgrade pip >nul 2>&1
    "%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: could not install pyserial. Check your internet connection.
        pause
        exit /b 1
    )
    echo Requirements installed.
) else (
    echo Requirements already present.
)
echo.

echo ============================================
echo    Starting the Controller Probe...
echo ============================================
echo.

:launch
"%VPY%" controller_probe.py %*
set "RC=%ERRORLEVEL%"
rem Flip the flag on a confirmed (clean) run so setup is skipped next time.
if "%RC%"=="0" if not exist "venv\.setup_ok" echo ok> "venv\.setup_ok"
echo.
if not "%RC%"=="0" echo Controller Probe exited with error %RC%.
pause
exit /b %RC%

rem ---- helper: set PYCMD if "%*" is Python and reports >= 3.10 -------------
:try_python
%* -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=%*"
goto :eof
