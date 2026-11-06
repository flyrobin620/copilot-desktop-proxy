@echo off
setlocal
rem One-time Microsoft sign-in; afterwards the Copilot desktop app is not needed.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\install.ps1" -SignIn -NoPull %*
set "taskResult=%ERRORLEVEL%"
pause
exit /b %taskResult%
