@echo off
setlocal
rem Install or update in one run. Options, e.g.: Install.cmd -SignIn
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set "taskResult=%ERRORLEVEL%"
pause
exit /b %taskResult%
