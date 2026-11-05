@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude-desktop.ps1" -Mode Start
set "taskResult=%ERRORLEVEL%"
if not "%taskResult%"=="0" pause
exit /b %taskResult%
