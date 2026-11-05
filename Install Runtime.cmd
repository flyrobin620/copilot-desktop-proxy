@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-runtime.ps1"
if errorlevel 1 (
  echo Runtime setup failed. Review the message above.
  pause
  exit /b 1
)
pause
