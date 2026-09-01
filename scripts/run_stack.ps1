# run_stack.ps1 - AITCH demo stack launcher (M2, 2026-07-24)
# Usage:  .\scripts\run_stack.ps1
# Brings up: docker infra (kafka/postgres/qdrant/sink + spc-seed/spc-consumer) + app windows.
# NOTE: B-SPC now runs as a compose service (spc-consumer, B6-2) - do NOT also start it here
#       (same consumer group = double-boot, split partitions).
# Operator then only needs the browser (http://localhost:5173) - RUN/approve are buttons.
# NOTE: ASCII-only on purpose (PS 5.1 reads non-BOM UTF-8 as ANSI - see CLAUDE.md sec.7).
# C agent service is commented until LLM env vars are set in .env (AGENT_*).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[1/3] docker compose up -d (infra + topics + sink)..."
# 2026-08-08 (#142): --scale agent-service=0 REMOVED. The blockers it guarded are closed:
#   (a) empty hf_cache -> hf-seed one-shot restores bge-m3 from S3 and agent-service waits
#       on service_completed_successfully;
#   (b) group mismatch -> compose now pins KAFKA_CONSUMER_GROUP=-demo + OFFSET_RESET=latest,
#       same as the old host process (which is gone from stack.services in this PR);
#   (c) stub quantitative values -> AGENT_API_MODE=real, with the three materials shipped
#       (.dockerignore negations for B modules, COPY src/common/, sqlalchemy).
#   The container is now the only c-agent. Re-adding the flag brings back zero-evidence Briefs.
docker compose up -d

$dsn = "postgresql://fdc_admin:change-me@localhost:5432/fdc_platform"

$procs = @(
    # B-SPC removed - runs as compose service spc-consumer (B6-2). See NOTE above.
    # A-PRED / GROUPER removed (2026-08-07, #130 follow-up) - both run as compose services
    #   now (a-pred, grouper). Same rule as B-SPC: starting them here too = 2 consumers in
    #   one group = one works, one silently idles (fdc.alert is single-partition).
    # C-AGENT removed (2026-08-08, #142) - runs as compose service agent-service. Same rule
    #   as B-SPC / A-PRED / GROUPER: starting it here too puts two consumers on fdc.alert.
    #   Unlike those, a mismatch here is worse than idling - if the groups differ (-demo vs
    #   default) BOTH process every alert and duplicate Briefs are actually created.
    #   The commented-out host entry was dropped with the container handover - do not restore.
    @{ T = "GATEWAY";  C = "uvicorn src.gateway.main:app --port 8000" },
    # CT1 daily sliding retrain orchestrator (constitution 3-3 (1); design CT1_wiring_v1 D1).
    # Resident, not a Kafka consumer. Runs dry-run only until ct.ct1_auto_generate is true (S1).
    # Must run in an interactive console window - WDAC blocks native DLLs in detached procs (TSR-0001).
    @{ T = "CT1-ORCH"; C = "python ct1_orchestrator.py"; D = "src/agent_a_mlops/lean85" },
    @{ T = "FRONTEND"; C = "npm run dev"; D = "frontend" }
)

# NOTE (2026-08-08): do NOT add AE_BUNDLE_DIR to the $env: block below. a-pred is a compose
#   service since #130, so these settings never reach the AE consumer - they only apply to the
#   GATEWAY / FRONTEND windows started here. AE knobs (LEAN85_AE_MODE, AE_BUNDLE_DIR) live in
#   docker-compose.yml -> a-pred.environment, fed from the repo-root .env.
#   The LEAN85_AE_MODE line below is likewise inert for AE; left untouched (PM-owned file).
Write-Host "[2/3] launching app windows..."
foreach ($p in $procs) {
    $wd = if ($p.D) { Join-Path $root $p.D } else { $root }
    $cmd = "`$host.UI.RawUI.WindowTitle='$($p.T)'; " +
           "Set-Location '$wd'; " +
           "`$env:DATABASE_URL='$dsn'; " +
           "`$env:KAFKA_BOOTSTRAP='localhost:9092'; " +
           "`$env:LEAN85_AE_MODE='live'; " +
           $p.C
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd | Out-Null
    Start-Sleep -Milliseconds 400
}

Write-Host "[3/3] done. open http://localhost:5173  (S11 RUN -> S3/S4 approve)"
Write-Host "stop: close windows + 'docker compose down' (add -v to reset DB)"
