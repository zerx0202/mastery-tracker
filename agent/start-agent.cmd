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

for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set REV=%%i
echo wersja agenta: %REV%

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo === pierwsze uruchomienie: tworze srodowisko ===
  py -3 -m venv .venv
  if errorlevel 1 goto :nopython
)

.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo UWAGA: nie udalo sie zainstalowac zaleznosci.
)

echo.
.venv\Scripts\python.exe agent.py
goto :end

:nopython
echo.
echo BLAD: nie znaleziono Pythona. Zainstaluj z python.org albo:
echo   winget install --id Python.Python.3.12 -e

:end
echo.
echo Agent zakonczyl prace.
pause
