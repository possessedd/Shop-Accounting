@echo off
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY goto nopy
:loop
%PY% app.py
echo Stopped. Restarting in 5 seconds. Close this window to exit.
timeout /t 5 >nul
goto loop
:nopy
echo Python not found. Install it from python.org and tick "Add python.exe to PATH".
pause
