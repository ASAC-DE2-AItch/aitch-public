#Requires -Version 5.1
<#
.SYNOPSIS
  RTD 자동정지 스위치(`rtd.auto_inhibit`)를 AWS 노드에서 켜고 끈다.

.DESCRIPTION
  시연 절차상 **PM 관측 구간에서만** 자동정지를 잠깐 꺼야 한다.

  왜 (2026-08-13 실측):
    · 광폭 관리선의 **중심**은 PHASE_0 동안 챔버가 흘린 웨이퍼 평균이다
      (`provisional_mode._center_for` — 표본 부족 시 old_center 폴백).
    · 자동정지가 챔버를 세우면 그 재료가 0이라 중심이 안 움직이고,
      **값이 관리선 안으로 영원히 안 들어온다.**
    · R9 로 풀어도 스톰 이동이 남아 **즉시 재정지**된다(실측: id=17 해제 → id=19 생성).
      → 스위치를 끄지 않으면 이 경로가 구조적으로 막힌다.

  안전:
    · 재시작 대상은 **grouper 하나**다. 게이트웨이가 아니므로 **시뮬레이터가 안 죽는다**.
    · grouper 는 `enable.auto.commit=False` + `ON CONFLICT DO NOTHING` 이라 재시작이 안전하다.
    · 이 스크립트는 **파일만 바꾼다** — 이미 걸린 정지는 안 푼다(해제는 R9 하류만, 헌법 1-1 예외 4 ⓒ).

.PARAMETER Off
  자동정지 끄기 (PM 관측 구간 진입 전).
.PARAMETER On
  자동정지 켜기 (기본 상태로 복귀). **시연 종료 후 반드시 실행.**
.PARAMETER Status
  현재 값만 조회 (변경 없음).

.EXAMPLE
  .\scripts\rtd_switch.ps1 -Status
  .\scripts\rtd_switch.ps1 -Off     # 대PM 주입 직전
  .\scripts\rtd_switch.ps1 -On      # 시연 끝나고
#>
[CmdletBinding()]
param(
    [switch]$Off,
    [switch]$On,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$REGION   = "ap-northeast-2"
$TAG      = "aitch-app"
$INSTANCE = "i-0d2475f34665442c2"

if (-not ($Off -or $On -or $Status)) {
    Write-Host "사용법: -Status | -Off | -On" -ForegroundColor Yellow
    exit 2
}

# ── 원격 명령 조립 ─────────────────────────────────────────────────────────
#   ⚠️ SSM 파라미터 파일은 **ASCII 전용**이다 — 한글이 들어가면 AWS CLI 가
#      cp949 로 디코드하려다 죽는다 (2026-08-13 하루 7회 실측).
$target = if ($Off) { "false" } elseif ($On) { "true" } else { $null }

$cmds = @(
    "cd /opt/aitch",
    "echo '=== current ==='",
    "grep -n 'auto_inhibit:' config/params.yaml | head -2"
)

if ($target) {
    $from = if ($Off) { "true" } else { "false" }
    $cmds += @(
        "echo '=== flip to $target ==='",
        "python3 -c `"import re,io;p='config/params.yaml';s=io.open(p,encoding='utf-8').read();s2=re.sub(r'(?m)^(  auto_inhibit:\s*)$from', r'\1$target ', s, count=1);io.open(p,'w',encoding='utf-8').write(s2);print('  changed' if s2!=s else '  NO CHANGE (already $target)')`"",
        "grep -n 'auto_inhibit:' config/params.yaml | head -2",
        "echo '=== restart grouper only (gateway untouched - simulator survives) ==='",
        "docker restart fdc-grouper >/dev/null && echo '  grouper restarted'",
        "sleep 12"
    )
}

$cmds += @(
    "echo '=== container sees ==='",
    "docker exec fdc-grouper python3 -c `"import yaml;d=yaml.safe_load(open('/app/config/params.yaml',encoding='utf-8'));print('  auto_inhibit =', d['rtd']['auto_inhibit'])`"",
    "echo '=== simulator alive? ==='",
    "curl -s --max-time 10 http://localhost/api/simulator/status | head -c 160"
)

$payload = @{ commands = $cmds } | ConvertTo-Json -Depth 3 -Compress
$tmp = Join-Path $env:TEMP "rtd_switch_$PID.json"
[IO.File]::WriteAllText($tmp, $payload, (New-Object Text.UTF8Encoding $false))

try {
    $cid = aws ssm send-command --region $REGION --document-name "AWS-RunShellScript" `
             --targets "Key=tag:Name,Values=$TAG" --parameters "file://$tmp" `
             --timeout-seconds 300 --query "Command.CommandId" --output text
    Start-Sleep -Seconds $(if ($target) { 32 } else { 14 })
    $out = aws ssm get-command-invocation --region $REGION --command-id $cid `
             --instance-id $INSTANCE --query "StandardOutputContent" --output text
    Write-Host $out
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}

if ($Off) {
    Write-Host ""
    Write-Host "자동정지 OFF — 대PM(시나리오 2)을 주입하세요." -ForegroundColor Cyan
    Write-Host "시연이 끝나면 반드시 -On 으로 되돌리세요." -ForegroundColor Yellow
}
