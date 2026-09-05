@echo off
cd /d "%~dp0"
echo Installing Python environment...
py -3.12 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" (echo venv failed. Install Python 3.12+ from python.org ^(check Add to PATH^). ^& pause ^& exit /b 1)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install playwright
echo Done. Next: fill monitor_config.json, run 1-start-chrome.bat, log in, then 2-start-monitor.bat
pause
