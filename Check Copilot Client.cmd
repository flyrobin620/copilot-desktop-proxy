@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure-copilot-client.ps1" -Mode Status
set "configureResult=%ERRORLEVEL%"
pause
exit /b %configureResult%
