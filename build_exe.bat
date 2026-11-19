@echo off
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY goto nopy
%PY% -m pip install pyinstaller -r requirements.txt
%PY% -m PyInstaller --onefile --name MagazinOblik app.py
echo.
if exist dist\MagazinOblik.exe (echo DONE: dist\MagazinOblik.exe) else (echo FAILED: see messages above)
pause
exit /b 0
:nopy
echo Python not found. Install it from python.org and tick "Add python.exe to PATH", then run this file again.
pause
exit /b 1
