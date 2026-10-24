@echo off
rem Double-click launcher: same as `paper-bot stop` (protective stops stay at IBKR).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_paperbot.ps1" %*
pause
