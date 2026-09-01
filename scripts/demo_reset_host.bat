@echo off
chcp 65001 >nul
REM 회차 초기화 원클릭 (더블클릭용) — 상세는 demo_reset_host.ps1
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\demo_reset_host.ps1"
