@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\claude-desktop.ps1" -Mode Restore
set "taskResult=%ERRORLEVEL%"
pause
exit /b %taskResult%
