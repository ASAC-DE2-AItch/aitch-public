@echo off
REM Re-run only analyze + make_report on an existing run (no refit, seconds).
REM Usage: run_reanalyze.bat [tag]   default tag = main
setlocal
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PYTHONUTF8=1
cd /d "%~dp0"
set "PY=%~dp0..\..\venv\Scripts\python.exe"
set TAG=%1
if "%TAG%"=="" set TAG=main
set "LOG=%~dp0out\run_reanalyze.log"

echo [1/2] analyze.py
"%PY%" analyze.py --tag %TAG% > "%LOG%" 2>&1
if errorlevel 1 goto :fail
echo       ok
echo [2/2] make_report.py
"%PY%" make_report.py --tag %TAG% >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
echo       ok
echo.
echo DONE - out\run_%TAG%\metrics.json refreshed
pause
exit /b 0
:fail
echo [FAILED] see %LOG%
pause
exit /b 1
