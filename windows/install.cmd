@echo off
rem Double-click to install Claude Pulse from a downloaded copy of the repo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Source "%~dp0.."
echo.
pause
