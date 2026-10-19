@echo off
rem Huragok, the Engineer: the one way to start your servers from now on.
rem Double-click it: the window opens and starts every server in SERVERS\.
rem Closing the window leaves the servers running; opening it again picks them up.
rem The same script runs Huragok's other commands:
rem   start.cmd status                 what each server is doing
rem   start.cmd stop [server] [--now]  stop servers (none named: all, then Huragok)
rem   start.cmd close                  close Huragok; the servers keep running
rem   start.cmd restart <server>       restart a server, warning its players first
rem   start.cmd say all "text"         a chat line on every server
rem   start.cmd rcon players           one console command
rem   start.cmd watch <server>         follow a server's console output
rem   start.cmd check                  check the settings, servers and commands
rem   start.cmd update                 look for a new version, and install it if you say yes
rem   start.cmd start --no-menu        run with no dashboard (for a scheduled task at boot)
rem The first start sets up Python packages in engine\.venv (needs Python 3.11+).
setlocal
cd /d "%~dp0"
set "ENGINE=%~dp0engine"
set "VENV=%~dp0engine\.venv"
set "PYTHON=%~dp0engine\.venv\Scripts\python.exe"
set "HURAGOK_LAUNCHER=start.cmd"

if not exist "%PYTHON%" goto :make_venv
rem A venv whose Python was uninstalled or moved can't run at all: it's made again.
"%PYTHON%" -c "" >nul 2>&1 && goto :packages
echo engine\.venv doesn't work any more (was Python uninstalled or moved?): setting it up again
rmdir /s /q "%VENV%" >nul 2>&1

:make_venv
call :find_python
if not defined BASE_PYTHON goto :no_python
echo Setting up for the first time: Python packages go in engine\.venv
%BASE_PYTHON% -m venv "%VENV%" || goto :failed
del "%VENV%\requirements.txt" >nul 2>&1

:packages
set "PYTHONPATH="
fc /b "%ENGINE%\requirements.txt" "%VENV%\requirements.txt" >nul 2>&1 && goto :probe
echo Installing the Python packages the engine needs...
"%PYTHON%" -m pip install --disable-pip-version-check --quiet -r "%ENGINE%\requirements.txt" || goto :failed
copy /y "%ENGINE%\requirements.txt" "%VENV%\requirements.txt" >nul

:probe
set "PYTHONPATH=%ENGINE%"
rem Packages marked as installed that don't load (a venv made again, files removed): installed again, once.
"%PYTHON%" -c "import huragok, psutil, websockets" >nul 2>&1 && goto :run
if defined REPAIRED goto :broken
set "REPAIRED=1"
del "%VENV%\requirements.txt" >nul 2>&1
goto :packages

:run
rem Each line that runs Huragok also ends this script: an update replaces
rem this file while Huragok runs, and cmd reads a script a line at a time.
if "%~1"=="" goto :start
"%PYTHON%" -m huragok %* & call exit /b %%ERRORLEVEL%%

:start
title Huragok
rem --hold keeps a double-clicked window open if it stops with a problem.
"%PYTHON%" -m huragok start --hold & call exit /b %%ERRORLEVEL%%

:find_python
rem Each candidate is judged by its exit code alone. Reading its path back (for /f) would
rem garble a path with accents or other letters in it: cmd reads it in another code page.
set "BASE_PYTHON="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "BASE_PYTHON=py -3" && exit /b 0
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "BASE_PYTHON=python" && exit /b 0
python3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "BASE_PYTHON=python3"
exit /b 0

:no_python
echo.
echo Huragok needs Python 3.11 or newer, and didn't find it.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo or in a terminal:  winget install -e --id Python.Python.3.12
echo then start this again.
if "%~1"=="" pause
exit /b 1

:failed
echo.
echo Setting up the Python packages failed (see above). Check the internet connection
echo and start again. To start the setup over, delete the folder engine\.venv
if "%~1"=="" pause
exit /b 1

:broken
echo.
echo Huragok doesn't load, even with its Python packages installed again:
"%PYTHON%" -c "import huragok, psutil, websockets"
echo To set the packages up from the start, delete the folder engine\.venv and start
echo this again. If the error above is in Huragok's own files, download it again.
if "%~1"=="" pause
exit /b 1
