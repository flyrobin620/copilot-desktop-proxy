@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0one-click.ps1" -Mode Claude %*
if errorlevel 1 pause
