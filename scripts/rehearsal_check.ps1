# rehearsal_check.ps1 - M3 rehearsal auto-check (2026-07-28, upd 2026-08-01)
# Usage:  .\scripts\rehearsal_check.ps1
# Precondition: .\scripts\run_stack.ps1 already ran (infra + gateway + frontend up).
# Output: rehearsal_report_<stamp>.txt in repo root  -> paste path to PM/Claude for judgement.
# NOTE: ASCII-only on purpose (PS 5.1 reads non-BOM UTF-8 as ANSI - see CLAUDE.md sec.7).

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$API   = "http://localhost:8000"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$out   = Join-Path $PSScriptRoot "..\rehearsal_report_$stamp.txt"
$lines = New-Object System.Collections.ArrayList

function Log($msg) {
    Write-Host $msg
    [void]$lines.Add($msg)
}
function Get-Json($path) {
    # PS 5.1 quirk: JSON without charset is decoded as ISO-8859-1 -> Korean mojibake.
    # Re-decode raw bytes as UTF-8 before ConvertFrom-Json.
    try {
        $r = Invoke-WebRequest -Uri "$API$path" -TimeoutSec 10 -Method GET -UseBasicParsing
        $txt = [System.Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray())
        return $txt | ConvertFrom-Json
    }
    catch { Log ("  ! GET {0} FAILED: {1}" -f $path, $_.Exception.Message); return $null }
}
function Post-Json($path, $body) {
    try {
        $json = if ($body) { $body | ConvertTo-Json -Compress } else { "{}" }
        return Invoke-RestMethod -Uri "$API$path" -TimeoutSec 30 -Method POST -Body $json -ContentType "application/json"
    } catch { Log ("  ! POST {0} FAILED: {1}" -f $path, $_.Exception.Message); return $null }
}

Log "=== AITCH M3 REHEARSAL CHECK  $stamp ==="
Log ""

# --- 0. infra ---------------------------------------------------------------
Log "[0] docker services"
try {
    docker compose ps --format "{{.Service}}\t{{.State}}" 2>&1 | ForEach-Object { Log ("  " + $_) }
} catch { Log "  ! docker compose ps failed: $($_.Exception.Message)" }
Log ""

# --- 1. gateway alive -------------------------------------------------------
Log "[1] gateway /simulator/status"
$st0 = Get-Json "/simulator/status"
if ($null -eq $st0) { Log "  VERDICT: FAIL - gateway unreachable. Stop here."; $lines | Set-Content -Path $out -Encoding UTF8; exit 1 }
Log ("  running={0}  mode={1}  pid={2}" -f $st0.running, $st0.mode, $st0.pid)
if ($st0.mode -ne "persistent") { Log "  ! WARN: mode field missing/old -> worldstate branch not deployed?" }
Log ""

# --- 2. scenario library ----------------------------------------------------
Log "[2] scenario library"
$sc = Get-Json "/simulator/scenarios"
$ids = @()
if ($sc) { $ids = @($sc.scenarios | ForEach-Object { $_.id }) ; Log ("  " + ($ids -join ", ")) }
if ($ids.Count -lt 2) { Log "  ! WARN: need >=2 scenarios for the two-RUN test" }
Log ""

# --- 3. RUN #1  (expect: started+injected) ----------------------------------
$s1 = if ($ids.Count -ge 1) { $ids[0] } else { "drift_c11" }
Log "[3] RUN #1  -> $s1   (expect mode=started+injected)"
$r1 = Post-Json "/simulator/scenarios/$s1/run" @{}
if ($r1) { Log ("  mode={0}  running={1}  pid={2}  injected={3}" -f $r1.mode, $r1.running, $r1.pid, ($r1.injected.Count)) }
$pid1 = $r1.pid
Start-Sleep -Seconds 20
Log ""

# --- 4. world is producing --------------------------------------------------
Log "[4] raw feed after RUN #1 (world alive?)"
$raw = Get-Json "/raw/recent?n=5"
if ($raw) { Log ("  wafers={0}  connected={1}  rows_seen={2}  last_ts={3}" -f $raw.status.wafers, $raw.status.connected, $raw.status.rows_seen, $raw.status.last_ts) }
$pred = Get-Json "/predictions/recent?n=5"
if ($pred) { Log ("  predictions={0}" -f (@($pred.items).Count)) }
Log ""

# --- 5. RUN #2  (expect: injected, SAME pid) --------------------------------
$s2 = if ($ids.Count -ge 2) { $ids[1] } else { $s1 }
Log "[5] RUN #2  -> $s2   (expect mode=injected AND same pid = world NOT respawned)"
$r2 = Post-Json "/simulator/scenarios/$s2/run" @{}
if ($r2) {
    Log ("  mode={0}  pid={1}  injected={2}" -f $r2.mode, $r2.pid, ($r2.injected.Count))
    if ($r2.mode -eq "injected" -and $r2.pid -eq $pid1) { Log "  -> PASS: persistent world kept (carry-over intact)" }
    else { Log "  -> FAIL: world respawned or mode wrong (carry-over broken)" }
}
Start-Sleep -Seconds 30   # full-stack: allow alert->incident path to land
Log ""

# --- 6. inject control dir --------------------------------------------------
Log "[6] control/inject state"
$ctl = Join-Path $PSScriptRoot "..\control\inject"
foreach ($sub in @(".", "processed", "failed")) {
    $p = Join-Path $ctl $sub
    if (Test-Path $p) {
        $n = @(Get-ChildItem -Path $p -Filter *.yaml -File -ErrorAction SilentlyContinue).Count
        Log ("  {0,-10} {1} file(s)" -f $sub, $n)
        if ($sub -eq "failed" -and $n -gt 0) {
            Get-ChildItem -Path $p -Filter *.yaml -File | ForEach-Object { Log ("     ! FAILED: " + $_.Name) }
        }
    } else { Log ("  {0,-10} (missing)" -f $sub) }
}
Log ""

# --- 7. gateway logs (injection trace) --------------------------------------
Log "[7] simulator logs tail (look for live-injection lines)"
$lg = Get-Json "/simulator/logs?n=40"
if ($lg) { $lg.lines | Select-Object -Last 25 | ForEach-Object { Log ("  | " + $_) } }
Log ""

# --- 8. M3 demo preconditions ----------------------------------------------
Log "[8] M3 demo preconditions"
# NOTE (2026-08-01): /limits/active filters sensor_window='settled' ON PURPOSE (gateway/main.py:716).
#   DB active total = 180 = settled 164 + transient 16 (C18/C27/C54/C56 x 4 chambers, step 4, C6_0).
#   So 164 here is CORRECT, not a seeding accident. Do NOT "fix" the gateway filter - the F3 version
#   chip must not mix transient-window limits into the settled control limits.
#   The window breakdown below is printed so this never gets misread as data loss again.
$lim = Get-Json "/limits/active"
if ($lim) {
    $prov = @($lim.limits | Where-Object { $_.trigger_type -eq "provisional" })
    Log ("  control_limits rows={0} (settled only - expect 164)  provisional rows={1}" -f (@($lim.limits).Count), $prov.Count)
}
# 2026-08-10 fix: `GROUP BY 1` pointed at the projected expression, which contains COUNT(*)
#   -> "aggregate functions are not allowed in GROUP BY". Group by the bare column instead.
#   Symptom was a printed ERROR line where the 164+16=180 breakdown should be, i.e. the one
#   check that guards against misreading 164 as data loss was itself dark. (rehearsal 8/10)
$qw = "SELECT sensor_window||'='||COUNT(*) FROM control_limits WHERE is_active GROUP BY sensor_window ORDER BY sensor_window;"
try {
    docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -t -A -c $qw 2>&1 |
        Where-Object { $_ -ne "" } | ForEach-Object { Log ("    DB active by window: " + $_) }
    Log "    (expect settled=164 transient=16 -> total 180)"
} catch { Log ("  ! window breakdown failed: " + $_.Exception.Message) }
$dis = Get-Json "/dispositions/pending?n=50"
if ($dis) { Log ("  dispositions pending={0}" -f (@($dis.dispositions).Count)) }
$qu = Get-Json "/quals/recent?n=1"
if ($qu) { Log ("  quals rows={0}" -f (@($qu.quals).Count)) }
$ap = Get-Json "/approvals/pending"
if ($ap) { Log ("  approvals pending={0}" -f (@($ap.pending).Count)) }

# SCRAP precondition: need a wafer with predicted_c65 > 1572 AND anomaly_score >= 0.5
Log "  -- SCRAP arm check (needs c65>1572 AND anomaly>=0.5, see decision doc 2026-07-28) --"
$pr = Get-Json "/predictions/recent?n=200"
if ($pr -and $pr.items) {
    $hi   = @($pr.items | Where-Object { $_.predicted_c65 -gt 1572 })
    $hiAn = @($pr.items | Where-Object { $_.anomaly_score -ge 0.5 })
    $both = @($pr.items | Where-Object { $_.predicted_c65 -gt 1572 -and $_.anomaly_score -ge 0.5 })
    $maxC = ($pr.items | Measure-Object -Property predicted_c65 -Maximum).Maximum
    $maxA = ($pr.items | Measure-Object -Property anomaly_score -Maximum).Maximum
    Log ("  c65>1572: {0}   anomaly>=0.5: {1}   BOTH: {2}   (max c65={3}, max anomaly={4})" -f $hi.Count, $hiAn.Count, $both.Count, $maxC, $maxA)
    if ($both.Count -eq 0) { Log "  -> NOTE: no SCRAP-eligible wafer yet. Inject the crazy/spike scenario, or raise spike size." }
}
Log ""

# --- 8b. DB-side pipeline counts (full-stack only meaningful) ----------------
Log "[8b] DB-side counts (docker psql) - alert->incident->approval landed?"
$q = "SELECT 'incidents='||COUNT(*) FROM incidents UNION ALL SELECT 'incident_alerts='||COUNT(*) FROM incident_alerts UNION ALL SELECT 'wafer_dispositions='||COUNT(*) FROM wafer_dispositions UNION ALL SELECT 'approval_records='||COUNT(*) FROM approval_records UNION ALL SELECT 'agent_reports='||COUNT(*) FROM agent_reports;"
try {
    docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -t -A -c $q 2>&1 |
        Where-Object { $_ -ne "" } | ForEach-Object { Log ("  " + $_) }
    $q2 = "SELECT incident_id||' | '||chamber_id||' | '||lifecycle||' | '||COALESCE(incident_type,'-')||' | '||to_char(created_at,'MM-DD HH24:MI') FROM incidents ORDER BY created_at DESC LIMIT 3;"
    Log "  recent incidents:"
    docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -t -A -c $q2 2>&1 |
        Where-Object { $_ -ne "" } | ForEach-Object { Log ("    " + $_) }
} catch { Log ("  ! psql failed: " + $_.Exception.Message) }
Log ""

# --- 9. KB round-trip (hf_cache channel) ------------------------------------
# 근거 0장 리포트는 **무중단으로 정상처럼** 나온다 (6-3 폴백이 의도대로 동작한 결과다).
# 프로세스 정상 · 화면에 Brief 있음 · 근거만 없음 — 밖에서는 성공과 구별되지 않는다.
# 런타임 계측(pipeline._note_kb_empty)은 이미 있지만 그건 **사후에 읽는 것**이고,
# 여기서 필요한 것은 **시작 전에 막는 게이트**다 (#144 · #130 회신2 PM 찬성).
# LLM 은 필요 없다 — KB 검색은 LLM 호출 전에 끝난다.
Log "[9] KB round-trip (hf_cache -> embedding -> Qdrant)"
try {
    # 2026-08-12 fix: agent-service is a BAKED image (no bind mount). If the probe module
    #   was added to the repo after the image was built, `python -m` exits rc=1 with
    #   "No module named ..." -- the SAME rc as a real evidence-0 FAIL. That conflates
    #   "cannot check" with "checked and failed" (CLAUDE.md sec.7: SKIP is not PASS/FAIL).
    #   Rehearsal 08-12 04:26 hit exactly this: probe missing -> report said "FAIL:
    #   evidence 0" while agent-service logs showed live bge_m3 queries to 3 Qdrant
    #   collections, all 200 OK. Pre-check the file so SKIP is reported as SKIP.
    docker exec fdc-agent-service ls /app/src/agent_service/kb_roundtrip_probe.py *> $null
    if ($LASTEXITCODE -ne 0) {
        Log "  -> SKIP: probe module not in container image (image predates probe). NOT a FAIL."
        Log "     KB verdict is UNKNOWN here - verify via logs instead:"
        Log "       docker logs fdc-agent-service 2>&1 | Select-String -Pattern 'qdrant.*200 OK|bge' | Select-Object -Last 5"
        Log "     (collection queries w/ 200 OK = embedding+Qdrant alive. Rebuild image to restore"
        Log "      this gate - but do NOT rebuild on demo day.)"
    } else {
        $kb = docker exec fdc-agent-service python -m src.agent_service.kb_roundtrip_probe 2>&1 |
              Where-Object { $_ -notmatch "Loading weights" }
        $kbRc = $LASTEXITCODE
        $kb | ForEach-Object { Log ("  " + $_) }
        if ($kbRc -eq 0)      { Log "  -> PASS: KB channel alive" }
        elseif ($kbRc -eq 1)  { Log "  -> FAIL: evidence 0 - briefs will be generated WITHOUT grounds" }
        else                  { Log "  -> WARN: probe could not judge (fixture/env) - see lines above" }
    }
} catch { Log ("  ! kb probe failed to run: " + $_.Exception.Message) }
Log ""

# --- 10. stop ---------------------------------------------------------------
Log "[10] STOP (end the world)"
$sp = Post-Json "/simulator/stop" @{}
if ($sp) { Log ("  running={0}" -f $sp.running) }
Log ""
Log "=== END ==="

$lines | Set-Content -Path $out -Encoding UTF8
Write-Host ""
Write-Host "report saved: $out"
