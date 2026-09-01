# ============================================================================
# llm_power.ps1 v2 - AITCH LLM EC2 (vLLM) power control with identical resume.
#
# Two tiers (pick by how long the box will sit idle):
#   stop/start  : -Off / -On    - EBS kept (200GB gp3 ~ $18/mo), resume 3-8 min.
#   full off    : -Down / -Up   - terraform destroy/apply, cost 0 while down,
#                                 recreate ~20 min (boot is fully automatic:
#                                 S3 weight restore + vLLM autostart).
#
# -Down/-Up REQUIRE the S3 backend migration (backend.tf, PR #90) to be done
# and a local terraform.tfvars + 'terraform init' in the infra dir. Until the
# CURRENT state holder has run 'terraform init -migrate-state', do NOT use
# -Up on another machine - two ledgers = ghost resources.
#
# Background (2026-08-02):
#   - vLLM is a docker container started WITHOUT a restart policy by the boot
#     script, so a bare stop/start leaves it Exited. -Off plants
#     '--restart unless-stopped' via SSM before stopping (template now has it
#     too for freshly applied boxes).
#   - No EIP -> public IP changes on every start/apply. 'terraform output'
#     goes stale after stop/start; this script resolves the instance by tag
#     (Name=aitch-llm-lab) and rewrites .env itself.
#
# Usage (from repo root):
#   .\scripts\llm_power.ps1 -Off       # stop now (plants restart policy first)
#   .\scripts\llm_power.ps1 -On        # start + new IP -> .env + ensure vllm + READY
#   .\scripts\llm_power.ps1 -Down      # terraform destroy (delegates teardown_llm.ps1)
#   .\scripts\llm_power.ps1 -Up        # terraform apply + .env + READY (~20 min)
#   .\scripts\llm_power.ps1 -Status    # state / ip / .env / vllm probe
#
# After -On or -Up says READY: restart the C-AGENT window (S11 stop -> start)
# so agent_service re-reads AGENT_LLM_BASE_URL from .env.
# ============================================================================

param(
  [switch]$On,
  [switch]$Off,
  [switch]$Up,
  [switch]$Down,
  [switch]$Status,
  [switch]$SkipIpCheck,
  [int]$MaxWaitMinutes = 0
)

$ErrorActionPreference = 'Continue'

$Region   = 'ap-northeast-2'
$Port     = 8000
$NameTag  = 'aitch-llm-lab'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvPath  = Join-Path $RepoRoot '.env'
$InfraDir = Join-Path $RepoRoot 'src\agent_service\infra\llm-ec2-experiment'

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
  Write-Host 'ERROR: aws cli not found in PATH.'
  exit 1
}

$picked = @()
if ($On)     { $picked += 'On' }
if ($Off)    { $picked += 'Off' }
if ($Up)     { $picked += 'Up' }
if ($Down)   { $picked += 'Down' }
if ($Status) { $picked += 'Status' }
if ($picked.Count -ne 1) {
  Write-Host 'Pick exactly one: -On | -Off | -Up | -Down | -Status'
  exit 1
}
if ($MaxWaitMinutes -le 0) {
  if ($Up) { $MaxWaitMinutes = 35 } else { $MaxWaitMinutes = 15 }
}

# ---- helpers ---------------------------------------------------------------

# Instance id by tag - survives recreation (terraform output id goes stale
# only across recreate; tag lookup is always current and needs no terraform).
function Get-InstanceId {
  $v = aws ec2 describe-instances --region $Region --filters ('Name=tag:Name,Values=' + $NameTag) 'Name=instance-state-name,Values=pending,running,stopping,stopped' --query 'Reservations[0].Instances[0].InstanceId' --output text 2>$null
  if ($LASTEXITCODE -ne 0) { return $null }
  if ($null -eq $v -or $v -eq 'None' -or [string]::IsNullOrWhiteSpace([string]$v)) { return $null }
  return ([string]$v).Trim()
}

function Get-InstanceField([string]$Id, [string]$Query) {
  $v = aws ec2 describe-instances --instance-ids $Id --region $Region --query $Query --output text 2>$null
  if ($LASTEXITCODE -ne 0) { return $null }
  if ($null -eq $v -or $v -eq 'None' -or [string]::IsNullOrWhiteSpace([string]$v)) { return $null }
  return ([string]$v).Trim()
}

function Get-TfOutput([string]$Name) {
  $v = terraform ('-chdir=' + $InfraDir) output -raw $Name 2>$null
  if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$v)) { return $null }
  return ([string]$v).Trim()
}

function Get-EnvLine([string]$Key) {
  if (-not (Test-Path $EnvPath)) { return $null }
  foreach ($l in [System.IO.File]::ReadAllLines($EnvPath)) {
    if ($l.StartsWith($Key + '=')) { return $l }
  }
  return $null
}

function Set-EnvKV([string]$Key, [string]$Value) {
  $lines = @()
  if (Test-Path $EnvPath) { $lines = [System.IO.File]::ReadAllLines($EnvPath) }
  $found = $false
  $out = New-Object System.Collections.Generic.List[string]
  foreach ($l in $lines) {
    if ($l.StartsWith($Key + '=')) { $out.Add($Key + '=' + $Value); $found = $true }
    else { $out.Add($l) }
  }
  if (-not $found) { $out.Add($Key + '=' + $Value) }
  $enc = New-Object System.Text.UTF8Encoding($false)     # BOM-less (dotenv safety)
  [System.IO.File]::WriteAllLines($EnvPath, $out.ToArray(), $enc)
}

function Test-Vllm([string]$Ip) {
  try {
    $r = Invoke-RestMethod -Uri ('http://' + $Ip + ':' + $Port + '/v1/models') -TimeoutSec 5
    if ($r.data -and $r.data.Count -ge 1) { return [string]$r.data[0].id }
    return $null
  } catch { return $null }
}

function Invoke-SsmCommands([string]$Id, [string[]]$Commands, [string]$Label) {
  $ping = $null
  for ($i = 0; $i -lt 24; $i++) {
    $ping = aws ssm describe-instance-information --filters ('Key=InstanceIds,Values=' + $Id) --region $Region --query 'InstanceInformationList[0].PingStatus' --output text 2>$null
    if ($ping -eq 'Online') { break }
    if ($i -eq 0) { Write-Host ('[' + $Label + '] waiting for SSM agent...') }
    Start-Sleep -Seconds 10
  }
  if ($ping -ne 'Online') {
    Write-Host ('[' + $Label + '] WARN: SSM agent not online - step skipped.')
    return $false
  }
  $tmp = Join-Path $env:TEMP 'llm_power_ssm.json'
  $json = @{ commands = $Commands } | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllLines($tmp, @($json), (New-Object System.Text.UTF8Encoding($false)))
  $cmdId = aws ssm send-command --instance-ids $Id --region $Region --document-name 'AWS-RunShellScript' --parameters ('file://' + $tmp) --query 'Command.CommandId' --output text
  if ($LASTEXITCODE -ne 0 -or -not $cmdId) {
    Write-Host ('[' + $Label + '] WARN: SSM send-command failed.')
    return $false
  }
  aws ssm wait command-executed --command-id $cmdId --instance-id $Id --region $Region 2>$null
  $st = aws ssm get-command-invocation --command-id $cmdId --instance-id $Id --region $Region --query 'Status' --output text 2>$null
  Write-Host ('[' + $Label + '] SSM result: ' + $st)
  return ($st -eq 'Success')
}

# Idempotent: add --restart unless-stopped to use_vllm.sh and to the container.
$DurableFixCmds = @(
  "grep -q 'restart unless-stopped' /usr/local/bin/use_vllm.sh 2>/dev/null || sed -i 's/docker run -d --name vllm/docker run -d --name vllm --restart unless-stopped/' /usr/local/bin/use_vllm.sh || true",
  "docker update --restart unless-stopped vllm || true"
)

function Wait-Ready([string]$Ip, [string]$Label) {
  Write-Host ('[' + $Label + '] waiting for vLLM model load (max ' + $MaxWaitMinutes + ' min)...')
  $deadline = (Get-Date).AddMinutes($MaxWaitMinutes)
  $model = $null
  while (-not $model -and (Get-Date) -lt $deadline) {
    $model = Test-Vllm $Ip
    if (-not $model) { Write-Host -NoNewline '.'; Start-Sleep -Seconds 15 }
  }
  Write-Host ''
  return $model
}

# ---------------------------------------------------------------- OFF -------
if ($Off) {
  $id = Get-InstanceId
  if (-not $id) { Write-Host '[off] no instance found (already terminated?). Nothing to stop.'; exit 0 }
  $state = Get-InstanceField $id 'Reservations[0].Instances[0].State.Name'
  Write-Host ('[off] instance ' + $id + ' state: ' + $state)
  if ($state -eq 'stopped') { Write-Host '[off] already stopped.'; exit 0 }
  if ($state -eq 'running') {
    $null = Invoke-SsmCommands $id $DurableFixCmds 'off'   # make next boot self-starting
  }
  aws ec2 stop-instances --instance-ids $id --region $Region --output text | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Host '[off] stop-instances failed (see error above).'; exit 1 }
  Write-Host '[off] stopping... (usually 1-3 min)'
  aws ec2 wait instance-stopped --instance-ids $id --region $Region
  if ($LASTEXITCODE -ne 0) { Write-Host '[off] wait failed - check the EC2 console.'; exit 1 }
  Write-Host '[off] STOPPED. Hourly charge ended; EBS (weights cache) kept (200GB gp3, ~$18/mo).'
  Write-Host '[off] resume: -On (3-8 min). Zero-cost instead: -Down (recreate -Up ~20 min).'
  exit 0
}

# ---------------------------------------------------------------- ON --------
if ($On) {
  $id = Get-InstanceId
  if (-not $id) {
    Write-Host '[on] no instance found - it was terminated/destroyed. Create with -Up'
    Write-Host '[on] (requires tfstate S3 backend migration + local terraform init, see README).'
    exit 1
  }
  $state = Get-InstanceField $id 'Reservations[0].Instances[0].State.Name'
  Write-Host ('[on] instance ' + $id + ' state: ' + $state)
  if ($state -ne 'running') {
    aws ec2 start-instances --instance-ids $id --region $Region --output text | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Write-Host '[on] start-instances failed. If state was "stopping", wait a minute and rerun.'
      exit 1
    }
    Write-Host '[on] starting...'
    aws ec2 wait instance-running --instance-ids $id --region $Region
    if ($LASTEXITCODE -ne 0) { Write-Host '[on] wait instance-running failed.'; exit 1 }
  }

  $ip = $null
  for ($i = 0; $i -lt 12 -and -not $ip; $i++) {
    $ip = Get-InstanceField $id 'Reservations[0].Instances[0].PublicIpAddress'
    if (-not $ip) { Start-Sleep -Seconds 5 }
  }
  if (-not $ip) { Write-Host '[on] no public ip after 60s - check the EC2 console.'; exit 1 }
  Write-Host ('[on] public ip: ' + $ip)

  Set-EnvKV 'AGENT_LLM_BASE_URL' ('http://' + $ip + ':' + $Port)
  Set-EnvKV 'AGENT_LLM_MODE' 'api'
  Write-Host ('[on] .env updated -> ' + (Get-EnvLine 'AGENT_LLM_BASE_URL'))

  # Belt and braces: even with the restart policy, ensure the container is up.
  $ensure = @('systemctl stop ollama 2>/dev/null || true') + $DurableFixCmds + @(
    'docker start vllm || /usr/local/bin/use_vllm.sh',
    'docker update --restart unless-stopped vllm || true'
  )
  $null = Invoke-SsmCommands $id $ensure 'on'

  $model = Wait-Ready $ip 'on'
  if ($model) {
    Write-Host ('[on] READY - model: ' + $model)
    Write-Host ('[on] endpoint: http://' + $ip + ':' + $Port + '  (.env already updated)')
    Write-Host '[on] NEXT: restart the C-AGENT window (S11 stop -> start) to pick up the new .env.'
    exit 0
  }
  Write-Host '[on] NOT READY within the wait window.'
  Write-Host ('[on] inspect: aws ssm start-session --target ' + $id + ' --region ' + $Region)
  Write-Host '[on]   then : sudo docker logs --tail 50 vllm'
  Write-Host '[on] if only THIS machine times out: your public IP may differ from the'
  Write-Host '[on]   security group allowed_cidrs (terraform var) - check SG inbound 8000.'
  exit 1
}

# ---------------------------------------------------------------- UP --------
if ($Up) {
  if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
    Write-Host '[up] ERROR: terraform not found in PATH.'
    exit 1
  }
  $tfvars = Join-Path $InfraDir 'terraform.tfvars'
  if (-not (Test-Path $tfvars)) {
    Write-Host '[up] ERROR: terraform.tfvars missing in the infra dir.'
    Write-Host '[up] Create it from terraform.tfvars.example. Current ops values:'
    Write-Host '[up]   instance_type="g6e.xlarge"  serving_engine="vllm"  root_volume_size=200'
    Write-Host '[up]   allowed_cidrs=[your IP/32, teammate IP/32]'
    exit 1
  }
  if (-not $SkipIpCheck) {
    $myip = ''
    try { $myip = ([string](Invoke-RestMethod -Uri 'https://checkip.amazonaws.com' -TimeoutSec 10)).Trim() } catch {}
    if ($myip) {
      $hit = Select-String -Path $tfvars -Pattern ([regex]::Escape($myip)) -Quiet
      if (-not $hit) {
        Write-Host ('[up] STOP: your current public IP ' + $myip + ' is not in terraform.tfvars allowed_cidrs.')
        Write-Host ('[up] Add "' + $myip + '/32" to allowed_cidrs (keep teammates too), then rerun.')
        Write-Host '[up] (Applying without it = box comes up but this machine cannot reach it.)'
        exit 1
      }
      Write-Host ('[up] ip check ok (' + $myip + ' is in allowed_cidrs)')
    } else {
      Write-Host '[up] WARN: could not determine current public IP - continuing (-SkipIpCheck to silence).'
    }
  }

  Write-Host '[up] terraform apply... (infra 1-2 min; box bootstrap ~20 min follows)'
  terraform ('-chdir=' + $InfraDir) apply -auto-approve
  if ($LASTEXITCODE -ne 0) {
    Write-Host '[up] terraform apply FAILED.'
    Write-Host '[up] first time on this machine? run once: terraform -chdir=<infra dir> init'
    Write-Host '[up] (S3 backend - do this only AFTER the state holder ran init -migrate-state.)'
    exit 1
  }

  $ep = Get-TfOutput 'vllm_endpoint'
  $ip = Get-TfOutput 'public_ip'
  if (-not $ep -or -not $ep.StartsWith('http')) {
    Write-Host ('[up] ERROR: vllm_endpoint output is "' + $ep + '" - serving_engine must be vllm|both.')
    exit 1
  }
  Set-EnvKV 'AGENT_LLM_BASE_URL' $ep
  Set-EnvKV 'AGENT_LLM_MODE' 'api'
  Write-Host ('[up] .env updated -> ' + (Get-EnvLine 'AGENT_LLM_BASE_URL'))

  # No SSM ensure here: first boot's user_data owns the startup sequence
  # (racing it mid-bootstrap could start vLLM against a half-restored cache).
  $model = Wait-Ready $ip 'up'
  if ($model) {
    Write-Host ('[up] READY - model: ' + $model)
    Write-Host '[up] NEXT: restart the C-AGENT window (S11 stop -> start) to pick up the new .env.'
    exit 0
  }
  Write-Host '[up] NOT READY within the wait window (bootstrap can take ~20 min + load).'
  Write-Host ('[up] watch bootstrap: aws ssm start-session --target ' + (Get-TfOutput 'instance_id') + ' --region ' + $Region)
  Write-Host '[up]   then : sudo tail -f /var/log/llm-bootstrap.log'
  exit 1
}

# ---------------------------------------------------------------- DOWN ------
if ($Down) {
  if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
    Write-Host '[down] ERROR: terraform not found in PATH.'
    exit 1
  }
  $td = Join-Path $InfraDir 'teardown_llm.ps1'
  if (-not (Test-Path $td)) { Write-Host '[down] ERROR: teardown_llm.ps1 not found.'; exit 1 }
  Write-Host '[down] terraform destroy via teardown_llm.ps1 (idempotent; also reverts .env to local)...'
  try {
    & $td
  } catch {
    Write-Host ('[down] FAILED: ' + $_.Exception.Message)
    Write-Host '[down] manual check: terraform -chdir=<infra dir> state list'
    exit 1
  }
  Write-Host '[down] DONE - cost is now 0 (no instance, no EBS). Recreate: -Up (~20 min).'
  exit 0
}

# ---------------------------------------------------------------- STATUS ----
if ($Status) {
  $id = Get-InstanceId
  if (-not $id) {
    Write-Host ('instance : none (terminated/destroyed) - create with -Up   [tag ' + $NameTag + ', ' + $Region + ']')
    $envLine = Get-EnvLine 'AGENT_LLM_BASE_URL'
    if ($envLine) { Write-Host ('.env     : ' + $envLine) }
    exit 0
  }
  $state   = Get-InstanceField $id 'Reservations[0].Instances[0].State.Name'
  $ip      = Get-InstanceField $id 'Reservations[0].Instances[0].PublicIpAddress'
  $envLine = Get-EnvLine 'AGENT_LLM_BASE_URL'
  Write-Host ('instance : ' + $state + '  (' + $id + ', ' + $Region + ')')
  if ($ip) { Write-Host ('public ip: ' + $ip) } else { Write-Host 'public ip: -' }
  if ($envLine) { Write-Host ('.env     : ' + $envLine) } else { Write-Host '.env     : (key missing)' }
  if ($state -eq 'running' -and $ip) {
    $model = Test-Vllm $ip
    if ($model) { Write-Host ('vllm     : UP - ' + $model) }
    else        { Write-Host 'vllm     : DOWN or still loading (probe failed)' }
    if ($envLine -and ($envLine -ne ('AGENT_LLM_BASE_URL=http://' + $ip + ':' + $Port))) {
      Write-Host 'WARN     : .env does not match the current ip - run -On before using the agent.'
    }
  }
  exit 0
}
