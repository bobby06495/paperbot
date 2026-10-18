@echo off
rem Double-click launcher for stop_paperbot.ps1 (bypasses the execution policy for this run only).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_paperbot.ps1" %*
pause
