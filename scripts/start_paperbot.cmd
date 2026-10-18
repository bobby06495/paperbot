@echo off
rem Double-click launcher for start_paperbot.ps1 (bypasses the execution policy for this run only).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_paperbot.ps1" %*
pause
