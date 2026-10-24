@echo off
rem Double-click launcher: same as `paper-bot start` (start IB Gateway and log in first).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_paperbot.ps1" %*
pause
