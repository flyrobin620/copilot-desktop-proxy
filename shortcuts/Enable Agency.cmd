@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\install-agency.ps1"
set "taskResult=%ERRORLEVEL%"
pause
exit /b %taskResult%
