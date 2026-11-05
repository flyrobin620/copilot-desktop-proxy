@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude-desktop.ps1" -Mode Configure
set "taskResult=%ERRORLEVEL%"
pause
exit /b %taskResult%
