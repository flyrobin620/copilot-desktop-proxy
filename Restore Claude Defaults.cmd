@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore-claude.ps1"
set "restoreResult=%ERRORLEVEL%"
pause
exit /b %restoreResult%
