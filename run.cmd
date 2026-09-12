@echo off
setlocal enabledelayedexpansion

:: Start di-replication-sync on Windows, without needing PowerShell.
::
::   run.cmd                  start (or restart) on 127.0.0.1:8766
::   run.cmd -Port 9000       somewhere else
::   run.cmd -Stop            shut the running one down and exit
::   run.cmd -NoBrowser       start it but do not open a tab
::   run.cmd --no-update-check    anything else goes to the app as it is
::
:: Only ASCII is echoed here: a console on cp850 or cp437 garbles anything else.
:: Core-owned (D-LAB-5 tool template): `copier update` rewrites this file.

set "PORT=8766"
set "BIND=127.0.0.1"
set "STOP_ONLY=0"
set "NO_BROWSER=0"
set "PASS="
set "PYTHONPATH="

:parse_args
if "%~1"=="" goto run_script
if /i "%~1"=="-Port" (
    if "%~2"=="" (echo   ! -Port needs a number & exit /b 2)
    set "PORT=%~2" & shift & shift & goto parse_args
)
if /i "%~1"=="-Bind" (
    if "%~2"=="" (echo   ! -Bind needs an address & exit /b 2)
    set "BIND=%~2" & shift & shift & goto parse_args
)
if /i "%~1"=="-Stop" (set "STOP_ONLY=1" & shift & goto parse_args)
if /i "%~1"=="-NoBrowser" (set "NO_BROWSER=1" & shift & goto parse_args)
set "PASS=!PASS! %1"
shift
goto parse_args

:run_script
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if "%STOP_ONLY%"=="1" (
    call :stop_server
    if errorlevel 1 exit /b 1
    exit /b 0
)

set "PY_VENV=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PY_VENV%" (
    where py >nul 2>&1 && (set "BOOTSTRAP=py") || (
        where python >nul 2>&1 && (set "BOOTSTRAP=python") || (
            echo   ! no Python found on PATH - install Python 3.10+ from python.org
            exit /b 1
        )
    )
    echo   creating .venv ...
    !BOOTSTRAP! -m venv .venv
    if errorlevel 1 (
        echo   ! could not create .venv - is Python 3.10+ installed?
        exit /b 1
    )
    "%PY_VENV%" -m pip install --quiet --upgrade pip
)
fc /b requirements.txt .venv\.requirements.txt >nul 2>&1
if errorlevel 1 (
    echo   installing requirements ...
    "%PY_VENV%" -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo   ! could not install requirements.txt - check your network or proxy
        exit /b 1
    )
    copy /y requirements.txt .venv\.requirements.txt >nul
)

:: do not start on top of a port we could not free
call :stop_server
if errorlevel 1 exit /b 1
echo   starting di-replication-sync on http://%BIND%:%PORT%
:: its own minimised window, so the server outlives this script when run.cmd
:: was double-clicked and its console closes
start "di-replication-sync" /min "%PY_VENV%" -m di_replication_sync.app --host %BIND% --port %PORT% %PASS%

:: one second a turn, so give up after fifteen
set "READY=0"
for /l %%i in (1,1,15) do (
    if "!READY!"=="0" (
        timeout /t 1 /nobreak >nul 2>&1
        curl -s --max-time 2 "http://127.0.0.1:%PORT%/api/health" >nul 2>&1 && set "READY=1"
    )
)

if "%READY%"=="0" (
    echo   ! di-replication-sync did not come up on port %PORT%
    exit /b 1
)

echo   ready
echo   stop it with the red Stop button, or:  run.cmd -Stop
if "%NO_BROWSER%"=="0" start "" "http://127.0.0.1:%PORT%/"
exit /b 0

:stop_server
:: Whatever holds the port is ours to stop - the same rule run.sh uses. Ask it
:: to close itself first so a write in flight can finish, then make sure.
set "HOLDER="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /r /c:":%PORT% *[^ ]* *LISTENING" 2^>nul') do (
    if not "%%a"=="0" set "HOLDER=%%a"
)
if not defined HOLDER (
    echo   nothing running on port %PORT%
    goto :eof
)
curl -s -X POST --max-time 2 "http://127.0.0.1:%PORT%/api/shutdown" >nul 2>&1
timeout /t 1 /nobreak >nul 2>&1

:: Ask whether the pid exists at all before asking what it is, or "already
:: gone" and "not ours" both look like the same answer.
tasklist /fi "pid eq %HOLDER%" 2>nul | findstr /i "%HOLDER%" >nul
if errorlevel 1 (
    echo   stopped pid %HOLDER%
    goto :eof
)
:: still there - only ever kill our own interpreter
tasklist /fi "pid eq %HOLDER%" 2>nul | findstr /i "python" >nul
if errorlevel 1 (
    echo   ! port %PORT% is held by pid %HOLDER%, which is not one of ours
    echo   ! close it, or start somewhere else:  run.cmd -Port 9000
    exit /b 1
)
echo   killing pid %HOLDER%
taskkill /f /pid %HOLDER% >nul 2>&1
goto :eof
