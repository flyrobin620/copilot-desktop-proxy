@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure-clients.ps1" -Clients Both
set "taskResult=%ERRORLEVEL%"
pause
exit /b %taskResult%
