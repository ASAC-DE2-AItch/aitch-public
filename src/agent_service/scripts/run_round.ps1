# run_round.ps1 — 평가 라운드 1회 (데이터 계층 확인 → 준비 검문 → 측정)
#
# 왜 있나 (2026-07-22):
#   라운드7·7b 를 도커가 꺼진 채로 돌렸다. 실행은 끝까지 '성공'했지만 Qdrant 가 없어
#   근거 카드가 세 도구 모두 0장이 됐고, 판정 정확도까지 함께 무너졌다. 그런데 로그에는
#   아무 에러도 없어서 '측정 불가'를 '나쁜 성적'으로 읽고 하루를 잃었다.
#   순서를 기억에 맡기지 않기 위해 스크립트로 고정한다.
#
# ⚠️ **전원(EC2 start/stop)은 이 스크립트가 하지 않는다** (2026-08-04, W26-ⓔ).
#   전원 정본은 `scripts/llm_power.ps1`(PM · PR #90) 하나다. 이 스크립트는 **켜져 있는지
#   확인만** 하고, 안 켜져 있으면 무엇을 치라고 알려주고 멈춘다.
#
#   왜 나눴나 — 구 버전은 `terraform apply` → `terraform output public_ip` 로 직접 관리했는데,
#   stop/start 하면 public IP 가 바뀌어 tfstate 가 낡는다. 낡은 IP 가 .env 에 박히면
#   **죽은 주소로 30분 폴링하다 '실패'** 로 끝난다 — 서버는 멀쩡한데. 게다가 구 기본 동작이
#   측정 후 `terraform destroy` 라, 플래그 하나 깜빡하면 가중치 캐시(EBS)까지 날아가
#   다음 라운드가 20분 재생성이 됐다. **전원은 사람이 명시적으로 다루는 편이 안전하다.**
#
# 사용:
#   # ① 켜기 (한 번만 · 3~8분)      — 라운드를 여러 번 돌 거면 켠 채로 둔다
#   .\scripts\llm_power.ps1 -On
#
#   # ② 측정 (몇 번이든)
#   .\src\agent_service\scripts\run_round.ps1 -Tag round12-realengine
#   .\src\agent_service\scripts\run_round.ps1 -Tag round12-realengine -Resume  # 중단분 이어서
#   .\src\agent_service\scripts\run_round.ps1 -Tag round13 -Concurrency 4
#
#   ⚠️ 태그는 **새 이름**을 쓴다 — 같은 태그면 notes/eval/supervisor_results_<tag>.jsonl 에
#      이어붙어 옛 라운드와 섞인다. 기사용(2026-08-04): round3~11 · round5b ·
#      round7b/7c · fewshot1~4 · probe · smoke10.
#   .\src\agent_service\scripts\run_round.ps1 -Tag round12-realengine -Resume        # 중단분 이어서
#   .\src\agent_service\scripts\run_round.ps1 -Tag round10 -Concurrency 4
#
#   # 픽스처 교체 (W9 대조 — alerts_v2 47건)
#   .\src\agent_service\scripts\run_round.ps1 -Tag round20-v2 -Fixtures alerts_v2
#
#   ⚠️ `-Fixtures` 없이 돌리면 기본 `fixtures/alerts` 다 (2026-08-08 신설). 그 전에는
#      alerts_v2 를 재려면 `eval_supervisor` 를 직접 불러야 했는데, 그러면 위 1·2단계
#      (Qdrant 기동 확인 · LLM 엔드포인트 실제 타진)를 통째로 건너뛴다 — 라운드 7·7b 를
#      날린 그 경로다. 재는 대상을 바꾸려고 가드를 버리는 일이 없도록 인자로 받는다.
#
#   # ③ 끄기 (반드시 · g6e.xlarge 는 시간당 $2 안팎)
#   .\scripts\llm_power.ps1 -Off
#
# ⚠️ 이 파일은 UTF-8 **with BOM** 으로 저장한다 — PowerShell 5.1 은 BOM 이 없으면
#   cp949 로 읽어 한글이 깨진다 (CLAUDE.md §7).
# ⚠️ **foreground 로 실행한다.** 백그라운드면 WDAC 가 torch 를 막아 KB 임베딩이 죽고
#   근거 0장으로 '성공'한다 — 측정이 조용히 무효가 된다 (docs/adr/TSR-0001).

[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$Tag,
  [switch]$Resume,
  [int]$Concurrency = 2,
  # 픽스처 세트 — 이름만("alerts_v2") 주면 `src/agent_service/fixtures/` 밑으로 해석하고,
  # 경로를 주면 그대로 쓴다. 미지정이면 eval_supervisor 기본값(`fixtures/alerts`).
  [string]$Fixtures
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..\..").Path

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)       { Write-Host "    OK — $msg" -ForegroundColor Green }
function Die($msg)      { Write-Host "    실패 — $msg" -ForegroundColor Red; exit 1 }

# --- 1. 데이터 계층 (이게 오늘의 교훈) --------------------------------------
Step 1 "도커 — Postgres · Qdrant"
Push-Location $RepoRoot
docker compose up -d postgres qdrant | Out-Null
if ($LASTEXITCODE -ne 0) { Pop-Location; Die "docker compose 실패" }

$deadline = (Get-Date).AddSeconds(90)
do {
  Start-Sleep -Seconds 3
  try   { $cols = (Invoke-RestMethod "http://localhost:6333/collections" -TimeoutSec 5).result.collections.name }
  catch { $cols = $null }
} while (-not $cols -and (Get-Date) -lt $deadline)

if (-not $cols) { Pop-Location; Die "Qdrant 가 안 뜬다 — docker compose logs qdrant" }
Ok "Qdrant 컬렉션 $($cols.Count)종: $($cols -join ', ')"
Pop-Location

# --- 2. LLM 준비 검문 (전원은 안 건드린다) -----------------------------------
Step 2 "LLM 엔드포인트 — 켜져 있는지 확인만"
$envPath = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envPath)) { Die ".env 가 없다" }
$hit = Select-String -Path $envPath -Pattern '^AGENT_LLM_BASE_URL=(.+)$'
if (-not $hit) { Die ".env 에 AGENT_LLM_BASE_URL 이 없다 —  .\scripts\llm_power.ps1 -Status" }
$base = $hit.Matches.Groups[1].Value.Trim()

# ⚠️ `.env` 값을 믿지 않고 **실제로 찔러본다.** 인스턴스를 껐다 켜면 public IP 가 바뀌는데
#    (EIP 없음) .env 가 낡아 있으면 죽은 주소가 그대로 남는다. 그 상태로 측정하면 전 건이
#    LLM 실패 → fallback 리포트가 되어 '나쁜 성적'처럼 보인다.
try {
  $m = Invoke-RestMethod "$base/v1/models" -TimeoutSec 10
} catch {
  Write-Host "    엔드포인트 무응답: $base" -ForegroundColor Red
  Write-Host "    → 켜져 있지 않거나 .env 가 낡았다. 전원은 이 스크립트가 다루지 않는다:" -ForegroundColor Yellow
  Write-Host "        .\scripts\llm_power.ps1 -Status     # 지금 상태" -ForegroundColor Yellow
  Write-Host "        .\scripts\llm_power.ps1 -On         # start + 새 IP -> .env + READY (3~8분)" -ForegroundColor Yellow
  Die "LLM 미준비 — 위 명령 후 다시 실행"
}
Ok "모델 $($m.data[0].id) · max_model_len $($m.data[0].max_model_len) @ $base"

# --- 3. 측정 ------------------------------------------------------------------
Step 3 "평가 라운드 실행 — tag=$Tag"
Push-Location $RepoRoot
$env:PYTHONIOENCODING = "utf-8"
$evalArgs = @("-m", "src.agent_service.eval_supervisor", "--real", "--tag", $Tag, "--concurrency", $Concurrency)
if ($Resume) { $evalArgs += "--resume" }

# 픽스처 교체 — **여기서 존재를 확인하고 죽는다.** eval_supervisor 도 검사하지만, 그쪽은
#   LLM 을 이미 켜둔 뒤라(시간당 $2) 오타 하나로 전원을 켠 채 실패하는 게 아깝다.
if ($Fixtures) {
  $fx = if (Test-Path $Fixtures) { (Resolve-Path $Fixtures).Path }
        else { Join-Path $RepoRoot "src\agent_service\fixtures\$Fixtures" }
  if (-not (Test-Path $fx -PathType Container)) { Pop-Location; Die "픽스처 디렉토리가 없다: $fx" }
  $n = (Get-ChildItem -Path $fx -Filter *.json -File).Count
  if ($n -eq 0) { Pop-Location; Die "픽스처에 *.json 이 없다: $fx" }
  $evalArgs += @("--fixtures", $fx)
  Ok "픽스처 $Fixtures — $n 건"
} else {
  Ok "픽스처 기본값 (fixtures/alerts) — 교체하려면 -Fixtures alerts_v2"
}
python @evalArgs      # eval 자체 preflight(LLM+Qdrant+PG)가 한 번 더 검문한다
$evalExit = $LASTEXITCODE
Pop-Location

# --- 4. 마무리 안내 (끄지 않는다) ---------------------------------------------
Step 4 "완료"
Write-Host "    ⚠️ 인스턴스는 켜진 채다 — 요금이 계속 나간다(g6e.xlarge ≈ 시간당 `$2)." -ForegroundColor Yellow
Write-Host "       더 돌릴 거면 그대로 두고, 끝났으면:" -ForegroundColor Yellow
Write-Host "         .\scripts\llm_power.ps1 -Off" -ForegroundColor Yellow

Write-Host "`n결과: src/agent_service/notes/eval/supervisor_results_$Tag.jsonl" -ForegroundColor Cyan
Write-Host "보기: notes/jsonl_viewer.html 에 드롭"
exit $evalExit
