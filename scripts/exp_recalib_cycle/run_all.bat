@echo off
REM ============================================================================
REM run_all.bat - recalibration-cycle backtest, full pipeline (design doc v1)
REM   ASCII-only on purpose: Windows console codepage mangles Hangul in .bat
REM   (CLAUDE.md ch.7). All Korean text lives in the Python logs / JSON reports.
REM
REM Usage:  run_all.bat            full run  (295 fits, expect ~30-90 min)
REM         run_all.bat smoke      20-day smoke test (fast sanity check)
REM
REM Progress: tail scripts\exp_recalib_cycle\out\run_all.log
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

if not exist "out" mkdir out
set "LOG=%~dp0out\run_all.log"
set MODE=%1
set LIMIT=
set TAG=main
if /I "%MODE%"=="smoke" (
  set LIMIT=--limit-days 20
  set TAG=smoke
)

echo ============================================================ > "%LOG%"
echo run_all.bat  mode=%MODE%  tag=%TAG%  start=%DATE% %TIME% >> "%LOG%"
echo ============================================================ >> "%LOG%"

echo.
echo [1/5] prep_data.py   - build wafer table + verify design doc section 2
echo ---- [1/5] prep_data.py ---- >> "%LOG%"
"%PY%" prep_data.py >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
echo       ok

echo [2/5] verify_leakage.py - constitution 1-3 checklist
echo ---- [2/5] verify_leakage.py ---- >> "%LOG%"
"%PY%" verify_leakage.py >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
echo       ok

echo [3/5] run_backtest.py - rolling-origin backtest (LONG; watch out\run_all.log)
echo ---- [3/5] run_backtest.py ---- >> "%LOG%"
"%PY%" run_backtest.py --tag %TAG% %LIMIT% >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
echo       ok

echo [4/5] analyze.py     - metrics, stats, verdict, figures
echo ---- [4/5] analyze.py ---- >> "%LOG%"
"%PY%" analyze.py --tag %TAG% >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
echo       ok

echo [5/5] make_report.py - result report draft
echo ---- [5/5] make_report.py ---- >> "%LOG%"
"%PY%" make_report.py --tag %TAG% >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
echo       ok

echo.
echo ==================== DONE ====================
echo Outputs: %~dp0out\run_%TAG%\
echo   metrics.json / daily_rmse.csv / fig_*.png / run_manifest.json
echo Report:  see docs\ (result report, filename in the log)
echo run_all.bat DONE %DATE% %TIME% >> "%LOG%"
pause
exit /b 0

:fail
echo.
echo [FAILED] see %LOG%
echo run_all.bat FAILED %DATE% %TIME% >> "%LOG%"
pause
exit /b 1
