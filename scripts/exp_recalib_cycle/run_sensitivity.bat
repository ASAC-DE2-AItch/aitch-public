@echo off
REM ============================================================================
REM run_sensitivity.bat - design doc section 5 / section 6 sensitivity runs.
REM   ASCII-only (CLAUDE.md ch.7). Run AFTER run_all.bat has produced run_main.
REM
REM   A) boundary buffer  : train set excludes the day right before the cutoff
REM                         (design doc section 5, "boundary +/-1 day buffer")
REM   B) seed 43, 44      : seed sensitivity (frozen seed 42 stays canonical)
REM
REM Each run is a full 295-fit backtest, so expect roughly 3x the main run.
REM ============================================================================
setlocal
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONUTF8=1

cd /d "%~dp0"
set "PY=%~dp0..\..\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] venv python not found: %PY%
  pause
  exit /b 1
)
if not exist "out\wafer_table_1y.parquet" (
  echo [ERROR] run run_all.bat first - wafer table missing
  pause
  exit /b 1
)
set "LOG=%~dp0out\run_sensitivity.log"
echo run_sensitivity start %DATE% %TIME% > "%LOG%"

echo [A] boundary buffer 1 day
"%PY%" verify_leakage.py --buffer-days 1 >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
"%PY%" run_backtest.py --tag buf1 --buffer-days 1 >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
"%PY%" analyze.py --tag buf1 >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
echo     ok

for %%S in (43 44) do (
  echo [B] seed %%S
  "%PY%" run_backtest.py --tag seed%%S --seed %%S >> "%LOG%" 2>&1
  if errorlevel 1 goto :fail
  "%PY%" analyze.py --tag seed%%S >> "%LOG%" 2>&1
  if errorlevel 1 goto :fail
  echo     ok
)

echo.
echo ==================== DONE ====================
echo [C] compare_runs.py
"%PY%" compare_runs.py main buf1 seed43 seed44
echo.
echo Summary: out\sensitivity_summary.json
echo run_sensitivity DONE %DATE% %TIME% >> "%LOG%"
pause
exit /b 0

:fail
echo [FAILED] see %LOG%
echo run_sensitivity FAILED %DATE% %TIME% >> "%LOG%"
pause
exit /b 1
