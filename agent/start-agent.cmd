@echo off
title Mastery Tracker - agent LCU
cd /d "%~dp0"

echo === aktualizacja z repo ===
git pull
if errorlevel 1 (
  echo.
  echo UWAGA: git pull nie powiodl sie - uruchamiam WERSJE LOKALNA.
  echo.
)

for /f %%i in ('git rev-parse --short HEAD') do set REV=%%i
echo wersja agenta: %REV%
echo.

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0lcu-agent.ps1"
echo.
echo Agent zakonczyl prace.
pause