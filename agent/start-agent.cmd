@echo off
title Mastery Tracker - agent LCU
cd /d "%~dp0"
echo Sprawdzam aktualizacje...
git pull --quiet 2>nul
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0lcu-agent.ps1"
echo.
echo Agent zakonczyl prace.
pause
