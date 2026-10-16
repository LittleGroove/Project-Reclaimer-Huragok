@echo off
rem Reclaimer Supervisor: the one way to start your servers from now on.
rem Double-click it, or have a Task Scheduler task run it at startup.
rem The same script runs the supervisor's other commands:
rem   start.cmd check          check the settings, servers and commands
rem   start.cmd stop           stop the supervisor and the servers it runs
rem   start.cmd update         look for a new version, and install it if you say yes
rem   start.cmd rcon players   run one console command on the server
rem The first start sets up Python packages in engine\.venv (needs Python 3.11+).
setlocal
cd /d "%~dp0"
set "ENGINE=%~dp0engine"
set "VENV=%~dp0engine\.venv"
set "PYTHON=%~dp0engine\.venv\Scripts\python.exe"
set "RECLAIMER_LAUNCHER=start.cmd"

if exist "%PYTHON%" goto :packages
call :find_python
if not defined BASE_PYTHON goto :no_python
echo Setting up for the first time: Python packages go in engine\.venv
"%BASE_PYTHON%" -m venv "%VENV%" || goto :failed

:packages
fc /b "%ENGINE%\requirements.txt" "%VENV%\requirements.txt" >nul 2>&1 && goto :run
echo Installing the Python packages the engine needs...
"%PYTHON%" -m pip install --disable-pip-version-check --quiet -r "%ENGINE%\requirements.txt" || goto :failed
copy /y "%ENGINE%\requirements.txt" "%VENV%\requirements.txt" >nul

:run
set "PYTHONPATH=%ENGINE%"
rem Each line that runs the supervisor also ends this script: an update replaces
rem this file while the supervisor runs, and cmd reads a script a line at a time.
if "%~1"=="" goto :start
"%PYTHON%" -m reclaimer_supervisor %* & call exit /b %%ERRORLEVEL%%

:start
title Reclaimer Supervisor
rem --hold keeps a double-clicked window open if it stops with a problem.
"%PYTHON%" -m reclaimer_supervisor start --hold & call exit /b %%ERRORLEVEL%%

:find_python
set "BASE_PYTHON="
for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable * (sys.version_info >= (3, 11)))" 2^>nul') do set "BASE_PYTHON=%%p"
if defined BASE_PYTHON exit /b 0
for /f "delims=" %%p in ('python -c "import sys; print(sys.executable * (sys.version_info >= (3, 11)))" 2^>nul') do set "BASE_PYTHON=%%p"
exit /b 0

:no_python
echo.
echo Reclaimer Supervisor needs Python 3.11 or newer, and didn't find it.
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
