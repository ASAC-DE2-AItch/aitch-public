@echo off
chcp 65001 >nul
REM ============================================================
REM  백엔드 재시작 원클릭 (2026-08-11) — 더블클릭용
REM  마운트 방식 서비스는 재시작만으로 최신 코드가 반영된다.
REM  (frontend 는 굽는 이미지라 여기 없음 — 재빌드는 별도 결정)
REM ============================================================
echo [restart] gateway / spc-consumer / a-pred / grouper / agent-service ...
docker restart fdc-gateway fdc-spc-consumer fdc-a-pred fdc-grouper fdc-agent-service
echo.
echo [status]
docker ps --format "{{.Names}}  {{.Status}}" | findstr fdc-
echo.
echo [health] gateway...
curl.exe -s -m 8 http://localhost:8000/health
echo.
pause
