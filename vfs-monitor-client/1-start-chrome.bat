@echo off
setlocal
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (echo Google Chrome not found. Install it or edit the path in this .bat. ^& pause ^& exit /b 1)
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%~dp0chrome-profile" "https://visa.vfsglobal.com/kaz/ru/ita/login"
