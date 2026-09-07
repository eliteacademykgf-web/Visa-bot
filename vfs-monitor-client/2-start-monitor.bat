@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first. ^& pause ^& exit /b 1)
:loop
".venv\Scripts\python.exe" cdp_monitor.py
if %errorlevel%==42 (echo Update applied, restarting... ^& goto loop)
echo Monitor stopped. You can close this window.
pause
