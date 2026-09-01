# ============================================================================
# demo_reset_host.ps1 — 회차 초기화 원클릭 (호스트판 · 2026-08-11)
#   왜: gateway 가 컨테이너로 이관되며 화면 버튼(demo_reset.py)의 docker 호출이
#       [Errno 2] 'docker' 로 죽는다(리허설 E2E 실측). 같은 6단계를 호스트에서 돈다.
#   순서 = src/gateway/demo_reset.py._work 와 동일 (순서가 규약 — 바꾸면 조용히 실패).
#   ⚠️ UTF-8 with BOM 저장 (헌법 7장 — PS 5.1 한글).
# ============================================================================
$ErrorActionPreference = "Continue"
chcp 65001 | Out-Null
$KB = "/opt/kafka/bin/kafka-consumer-groups.sh"
$TOPICS = @("fdc.raw", "fdc.alert", "fdc.prediction", "fdc.actual")

Write-Host "== 1/6 세계 정지 (시뮬 STOP)" -ForegroundColor Cyan
try { Invoke-RestMethod -Method Post -Uri http://localhost:8000/simulator/stop -TimeoutSec 10 | Out-Null
      Write-Host "   simulator stop 요청 완료" }
catch { Write-Host "   stop skip: $($_.Exception.Message)" }

Write-Host "== 2/6 컨슈머 정지" -ForegroundColor Cyan
docker stop fdc-spc-consumer fdc-a-pred fdc-grouper fdc-prediction-sink fdc-agent-service 2>$null

Write-Host "== 3/6 Kafka 오프셋 latest 리셋 (그룹 x 토픽 · --all-topics 금지)" -ForegroundColor Cyan
$groups = docker exec fdc-kafka $KB --bootstrap-server kafka:9093 --list 2>$null |
          Where-Object { $_.Trim() -ne "" }
$n = 0
foreach ($g in $groups) { foreach ($t in $TOPICS) {
    docker exec fdc-kafka $KB --bootstrap-server kafka:9093 --group $g.Trim() --topic $t `
        --reset-offsets --to-latest --execute 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $n++ }
} }
Write-Host "   그룹 $($groups.Count)개 · 리셋 $n group x topic"
if ($n -eq 0) { Write-Host "   ⚠️ 리셋 0건 — 컨슈머가 살아있을 수 있음" -ForegroundColor Yellow }

Write-Host "== 4/6 초기화 SQL (reset_demo_state.sql)" -ForegroundColor Cyan
docker cp scripts\reset_demo_state.sql fdc-postgres:/tmp/reset.sql
docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/reset.sql
if ($LASTEXITCODE -ne 0) { Write-Host "   ❌ SQL 실패 — 중단" -ForegroundColor Red; pause; exit 1 }

Write-Host "== 5/6 Qual 스냅샷 재시딩" -ForegroundColor Cyan
docker compose run --rm spc-seed python -m src.agent_b_spc.seed_qual_snapshots
if ($LASTEXITCODE -ne 0) { Write-Host "   ❌ 재시딩 실패 — σ-갭 기준 없으면 Qual 판정 불가" -ForegroundColor Red; pause; exit 1 }

Write-Host "== 6/6 컨슈머 재기동 (+조인 대기 20초)" -ForegroundColor Cyan
docker start fdc-prediction-sink fdc-spc-consumer fdc-a-pred fdc-grouper 2>$null
docker restart fdc-spc-consumer 2>$null    # 가한계 복구 상태를 새 DB 로 재파생
docker start fdc-agent-service 2>$null
Start-Sleep -Seconds 20

Write-Host ""
Write-Host "✅ 회차 초기화 완료 — S11 에서 시나리오 1 RUN → 90초 → 시나리오 2 순서로 시작" -ForegroundColor Green
docker ps --format "{{.Names}}  {{.Status}}" | Select-String fdc-
pause
