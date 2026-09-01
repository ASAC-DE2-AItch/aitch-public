# ============================================================================
# app_stack.ps1 - AITCH app infrastructure (EC2 + RDS + VPC) lifecycle driver.
#
# Sibling of scripts\llm_power.ps1, but for a DIFFERENT terraform state:
#
#   state key                            applier          dir
#   ---------------------------------------------------------------------
#   tfstate/llm-ec2-experiment.tfstate   PM only (~8/13)  src\agent_service\infra\llm-ec2-experiment
#   tfstate/app-stack.tfstate            C only           infra\app-stack   <-- this (repo root)
#
# The S3 backend has no lock, so the rule is "one applier per state key"
# (see src\agent_service\infra\llm-ec2-experiment\backend.tf header, PR #155). This script is
# hard-pinned to the app-stack directory and REFUSES to run anywhere else -
# a wrong 'destroy' here would otherwise reach the rehearsal vLLM node, which
# costs ~20 min to rebuild.
#
# Two tiers, same as llm_power.ps1 - pick by how long the stack will sit idle:
#
#   -Off / -On    stop/start the BILLABLE compute only (EC2 + RDS).
#                 Data survives (EBS + RDS storage). Resume in minutes.
#                 Leftover cost ~ $8/mo (60GB EBS + 20GB RDS storage + idle EIP).
#   -Down / -Up   terraform destroy/apply. Cost 0 while down, but the RDS
#                 contents go with it (a final snapshot is kept) and a fresh
#                 -Up needs seeding again.
#
# Rule of thumb: overnight / a day or two -> -Off. Days of silence -> -Down.
#
# Usage (from repo root):
#   .\scripts\app_stack.ps1 -Plan      # init + validate + plan   (no cost, no state change)
#   .\scripts\app_stack.ps1 -Up        # terraform apply          (COST STARTS HERE)
#   .\scripts\app_stack.ps1 -Off       # stop EC2 + RDS           (keep data, ~$8/mo)
#   .\scripts\app_stack.ps1 -On        # start RDS + EC2 again
#   .\scripts\app_stack.ps1 -Status    # what exists + power state + cost so far
#   .\scripts\app_stack.ps1 -Down      # terraform destroy        (final snapshot kept)
#   .\scripts\app_stack.ps1 -Ssm       # SSM session into the app node
#   .\scripts\app_stack.ps1 -Log       # tail /var/log/aitch-bootstrap.log via SSM
#
#   -Yes           skip the typed confirmation on -Up / -Down (CI / repeat smoke)
#   -SkipIpCheck   do not compare current public IP against allowed_cidrs
#
# Two things AWS does behind your back while -Off:
#   - a stopped RDS instance is AUTO-STARTED after 7 days. -Off is not a
#     substitute for -Down over a long break.
#   - an EIP attached to a STOPPED instance is billed (~$0.005/h). It is only
#     free while the instance is running.
# ============================================================================

param(
  [switch]$Plan,
  [switch]$Up,
  [switch]$On,
  [switch]$Off,
  [switch]$Down,
  [switch]$Sync,
  [switch]$Status,
  [switch]$Ssm,
  [switch]$Log,
  [switch]$Yes,
  [switch]$SkipIpCheck,
  [switch]$AllowEmptyBundle,
  [switch]$SkipCompose
)

$ErrorActionPreference = 'Continue'

$Region   = 'ap-northeast-2'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$InfraDir = Join-Path $RepoRoot 'infra\app-stack'
$TfVars   = Join-Path $InfraDir 'terraform.tfvars'

# Rough ap-northeast-2 on-demand rates, USD/hour. Used only for the running
# cost hint in -Status; not a billing source of truth.
# !! These pair with infrapp-stackariables.tf: app_instance_type / db_instance_class.
#    Change the type there and this number goes stale SILENTLY - -Status keeps
#    printing a confident wrong number, with nothing to signal it is wrong.
$RateEc2 = 0.4838   # c7i.2xlarge
$RateRds = 0.104    # db.t4g.medium

function Write-Head($text) {
  Write-Host ''
  Write-Host "=== $text ===" -ForegroundColor Cyan
}

function Fail($text) {
  Write-Host "ERROR: $text" -ForegroundColor Red
  exit 1
}

# ---- guards ----------------------------------------------------------------
$picked = @()
if ($Plan)   { $picked += 'Plan' }
if ($Up)     { $picked += 'Up' }
if ($On)     { $picked += 'On' }
if ($Off)    { $picked += 'Off' }
if ($Down)   { $picked += 'Down' }
if ($Status) { $picked += 'Status' }
if ($Sync)   { $picked += 'Sync' }
if ($Ssm)    { $picked += 'Ssm' }
if ($Log)    { $picked += 'Log' }
if ($picked.Count -ne 1) {
  Write-Host 'Pick exactly one: -Plan | -Up | -Off | -On | -Sync | -Status | -Down | -Ssm | -Log'
  exit 1
}

if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) { Fail 'terraform not found in PATH.' }
if (-not (Get-Command aws -ErrorAction SilentlyContinue))       { Fail 'aws cli not found in PATH.' }
if (-not (Test-Path $InfraDir))                                 { Fail "infra dir not found: $InfraDir" }

# Belt and braces: the whole point of the separate directory is that a destroy
# here can never reach the GPU stack. Refuse if the resolved path drifted.
if ((Split-Path -Leaf $InfraDir) -ne 'app-stack') {
  Fail "refusing to run: resolved dir is not app-stack ($InfraDir)"
}

Push-Location $InfraDir
try {

  # ---- helpers -------------------------------------------------------------
  function Initialize-Terraform {
    if (-not (Test-Path (Join-Path $InfraDir '.terraform'))) {
      Write-Head 'terraform init'
      terraform init -input=false
      if ($LASTEXITCODE -ne 0) { Fail 'terraform init failed.' }
    }
  }

  function Assert-TfVars {
    if (Test-Path $TfVars) { return }
    Write-Host ''
    Write-Host "terraform.tfvars is missing: $TfVars" -ForegroundColor Yellow
    Write-Host 'Create it first (allowed_cidrs is required and has no default):'
    Write-Host '    (Invoke-RestMethod https://checkip.amazonaws.com).Trim()'
    Write-Host '    copy terraform.tfvars.example terraform.tfvars'
    Fail 'terraform.tfvars required.'
  }

  # The code bundle is what makes /opt/aitch non-empty. If it is not on S3, the
  # boot script logs an error and the node comes up with nothing to run - the
  # failure is a line in a log nobody is watching, not a failed apply. So this
  # one BLOCKS instead of warning.
  function Assert-Bundle {
    if ($AllowEmptyBundle) {
      Write-Host 'bundle check skipped (-AllowEmptyBundle) - the node will come up empty.' -ForegroundColor Yellow
      return
    }
    $bucket = ('var.bundle_s3_bucket' | terraform console 2>$null) -replace '"', ''
    $key    = ('var.bundle_s3_key'    | terraform console 2>$null) -replace '"', ''
    $bucket = $bucket.Trim(); $key = $key.Trim()
    if ([string]::IsNullOrWhiteSpace($key)) {
      Write-Host 'bundle_s3_key is empty - boot will skip the tar step by design.' -ForegroundColor Yellow
      return
    }
    $len = aws s3api head-object --bucket $bucket --key $key --region $Region `
             --query 'ContentLength' --output text 2>$null
    if ($LASTEXITCODE -eq 0 -and $len -and $len -ne 'None') {
      $mb = [math]::Round([double]$len / 1MB, 1)
      Write-Host ("code bundle present: s3://{0}/{1} ({2} MB)" -f $bucket, $key, $mb) -ForegroundColor Green
      return
    }
    Write-Host ''
    Write-Host ("code bundle NOT found: s3://{0}/{1}" -f $bucket, $key) -ForegroundColor Red
    Write-Host 'Without it /opt/aitch stays empty - there is not even a docker-compose.yml to bring up,'
    Write-Host 'and the only sign is one ERROR line in /var/log/aitch-bootstrap.log.'
    Write-Host ''
    Write-Host 'Build and upload it first:'
    Write-Host '    .\scripts\make_app_bundle.ps1 -DryRun     # see what would ship'
    Write-Host '    .\scripts\make_app_bundle.ps1             # upload'
    Write-Host ''
    Write-Host 'Deliberately bringing up a bare node (quota / IAM smoke)? pass -AllowEmptyBundle.'
    Fail 'refusing to apply without a code bundle.'
  }

  # Floating IP: allowed_cidrs is baked into the SG, so a changed home IP
  # locks you out of the very screen you just deployed. Warn, never block.
  function Test-PublicIp {
    if ($SkipIpCheck) { return }
    if (-not (Test-Path $TfVars)) { return }
    try {
      $me = (Invoke-RestMethod https://checkip.amazonaws.com -TimeoutSec 8).Trim()
    } catch {
      Write-Host 'WARN: could not resolve current public IP (skipping check).' -ForegroundColor Yellow
      return
    }
    $tf = [System.IO.File]::ReadAllText($TfVars, [System.Text.Encoding]::UTF8)
    if ($tf -match [regex]::Escape($me)) {
      Write-Host "current public IP $me is present in terraform.tfvars" -ForegroundColor Green
    } else {
      Write-Host ''
      Write-Host "WARN: current public IP $me is NOT in terraform.tfvars." -ForegroundColor Yellow
      Write-Host '      The security group will not let you reach the screen.'
      Write-Host ("      Add {0}/32 to allowed_cidrs, or pass -SkipIpCheck if intended." -f $me)
    }
  }

  function Confirm-Or-Exit($word, $blurb) {
    if ($Yes) { return }
    Write-Host ''
    Write-Host $blurb -ForegroundColor Yellow
    $answer = Read-Host "Type $word to continue"
    if ($answer -ne $word) {
      Write-Host 'aborted.'
      exit 1
    }
  }

  function Get-Output($name) {
    $v = terraform output -raw $name 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $v
  }

  function Get-InstanceId {
    $id = Get-Output 'app_instance_id'
    if ([string]::IsNullOrWhiteSpace($id)) {
      Fail 'no app_instance_id in state - is the stack up? (.\scripts\app_stack.ps1 -Status)'
    }
    return $id
  }

  # Run a shell script on the node through SSM and wait for it. Used by -Sync
  # and -On so neither has to tell the operator to go type it by hand.
  # Returns $true on Success. Prints node stdout/stderr either way.
  function Invoke-NodeScript($id, $lines, $label, $maxMinutes) {
    $params = @{ commands = $lines } | ConvertTo-Json -Compress
    Write-Head $label
    $cmdId = aws ssm send-command --instance-ids $id --region $Region `
               --document-name 'AWS-RunShellScript' --parameters $params `
               --query 'Command.CommandId' --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($cmdId)) {
      Write-Host 'ssm send-command failed (is the SSM agent up yet?).' -ForegroundColor Yellow
      return $false
    }
    Write-Host "command id: $cmdId"

    $deadline = (Get-Date).AddMinutes($maxMinutes)
    $status = 'Pending'
    while ((Get-Date) -lt $deadline) {
      Start-Sleep -Seconds 5
      $status = aws ssm get-command-invocation --command-id $cmdId --instance-id $id `
                  --region $Region --query 'Status' --output text 2>$null
      Write-Host "  $status"
      if ($status -notin @('Pending', 'InProgress', 'Delayed')) { break }
    }

    aws ssm get-command-invocation --command-id $cmdId --instance-id $id `
      --region $Region --query 'StandardOutputContent' --output text
    if ($status -ne 'Success') {
      aws ssm get-command-invocation --command-id $cmdId --instance-id $id `
        --region $Region --query 'StandardErrorContent' --output text
      Write-Host ("node script ended as '{0}'." -f $status) -ForegroundColor Yellow
      return $false
    }
    return $true
  }

  function Get-Ec2State($id) {
    $s = aws ec2 describe-instances --instance-ids $id --region $Region `
           --query 'Reservations[0].Instances[0].State.Name' --output text 2>$null
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    return $s
  }

  function Get-RdsState($rds) {
    $s = aws rds describe-db-instances --db-instance-identifier $rds --region $Region `
           --query 'DBInstances[0].DBInstanceStatus' --output text 2>$null
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    return $s
  }

  # Wait until the RDS instance reaches a target status. Start/stop take several
  # minutes; returning early would make -On look done while the DSN still refuses.
  function Wait-Rds($rds, $target, $maxMinutes) {
    $deadline = (Get-Date).AddMinutes($maxMinutes)
    while ((Get-Date) -lt $deadline) {
      $s = Get-RdsState $rds
      Write-Host ("  rds: {0}" -f $s)
      if ($s -eq $target) { return $true }
      Start-Sleep -Seconds 20
    }
    return $false
  }

  # ---- modes ---------------------------------------------------------------
  if ($Plan) {
    Assert-TfVars
    Initialize-Terraform
    Test-PublicIp

    Write-Head 'terraform fmt -check'
    terraform fmt -check
    if ($LASTEXITCODE -ne 0) { Write-Host 'NOTE: files are not fmt-clean (run: terraform fmt)' -ForegroundColor Yellow }

    Write-Head 'terraform validate'
    terraform validate
    if ($LASTEXITCODE -ne 0) { Fail 'validate failed.' }

    Write-Head 'terraform plan'
    terraform plan -input=false -out=tfplan
    if ($LASTEXITCODE -ne 0) { Fail 'plan failed.' }

    Write-Host ''
    Write-Host 'plan only - nothing was created and no state was written.' -ForegroundColor Green
    Write-Host 'Next: .\scripts\app_stack.ps1 -Up   (this is where cost starts)'
    exit 0
  }

  if ($Up) {
    Assert-TfVars
    Initialize-Terraform
    Test-PublicIp
    Assert-Bundle

    Write-Head 'terraform plan'
    terraform plan -input=false -out=tfplan
    if ($LASTEXITCODE -ne 0) { Fail 'plan failed - not applying.' }

    $daily = [math]::Round(($RateEc2 + $RateRds) * 24, 2)
    Confirm-Or-Exit 'apply' @"
About to CREATE the app stack (EC2 c7i.2xlarge + RDS + EIP + VPC).

  Billing starts now and does not stop until you destroy:
    EC2 ~`$$RateEc2/h + RDS ~`$$RateRds/h  ->  roughly `$$daily per day left running.

  Operating rule: create -> verify -> destroy, in short cycles.
  This state key (tfstate/app-stack.tfstate) is C-solo applier - make sure
  nobody else is applying it right now (the S3 backend has no lock).
"@

    Write-Head 'terraform apply'
    terraform apply -input=false tfplan
    if ($LASTEXITCODE -ne 0) { Fail 'apply failed.' }

    Write-Head 'outputs'
    terraform output

    Write-Host ''
    Write-Host 'Boot is NOT finished when apply returns.' -ForegroundColor Yellow
    Write-Host 'The node still has to install docker, fill hf_cache (4.7GB) and unpack the S3 tar.'
    Write-Host 'Watch it:   .\scripts\app_stack.ps1 -Log'
    Write-Host ''
    Write-Host 'Reminder: the boot script does NOT run "docker compose up" on purpose -'
    Write-Host 'startup-order faults would hide inside the boot log. Bring services up by hand.'
    exit 0
  }

  # ---- power: stop/start the billable compute only ------------------------
  # VPC / subnets / SGs / endpoints / SSM parameters cost nothing, so they stay.
  # Only EC2 and RDS are touched - the two line items that actually bill.
  if ($Off) {
    Initialize-Terraform
    $id  = Get-InstanceId
    $rds = Get-Output 'db_instance_id'

    Write-Head 'stopping billable compute'

    $ec2State = Get-Ec2State $id
    if ($ec2State -eq 'stopped' -or $ec2State -eq 'stopping') {
      Write-Host "  ec2: already $ec2State"
    } else {
      aws ec2 stop-instances --instance-ids $id --region $Region --output text | Out-Null
      if ($LASTEXITCODE -ne 0) { Fail 'ec2 stop failed.' }
      Write-Host '  ec2: stop requested'
    }

    if ([string]::IsNullOrWhiteSpace($rds) -or $rds -eq 'null') {
      Write-Host '  rds: not in state (enable_rds=false) - nothing to stop'
    } else {
      $rdsState = Get-RdsState $rds
      if ($rdsState -eq 'stopped' -or $rdsState -eq 'stopping') {
        Write-Host "  rds: already $rdsState"
      } else {
        aws rds stop-db-instance --db-instance-identifier $rds --region $Region --output text | Out-Null
        if ($LASTEXITCODE -ne 0) {
          Write-Host '  rds: stop failed (an instance that is not "available" cannot be stopped)' -ForegroundColor Yellow
        } else {
          Write-Host '  rds: stop requested'
        }
      }
    }

    Write-Host ''
    Write-Host 'compute billing stops once both reach stopped.' -ForegroundColor Green
    Write-Host 'Still billed while off: EBS 60GB + RDS storage 20GB + idle EIP  ~ $8/mo.'
    Write-Host 'AWS AUTO-STARTS a stopped RDS after 7 days - for a longer break use -Down.'
    Write-Host 'Resume: .\scripts\app_stack.ps1 -On'
    exit 0
  }

  if ($On) {
    Initialize-Terraform
    $id  = Get-InstanceId
    $rds = Get-Output 'db_instance_id'

    Write-Head 'starting billable compute'

    # RDS first: the app node's containers fail fast against a dead DSN, and
    # RDS takes far longer to come up than EC2.
    if (-not ([string]::IsNullOrWhiteSpace($rds) -or $rds -eq 'null')) {
      $rdsState = Get-RdsState $rds
      if ($rdsState -eq 'available' -or $rdsState -eq 'starting') {
        Write-Host "  rds: already $rdsState"
      } else {
        aws rds start-db-instance --db-instance-identifier $rds --region $Region --output text | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'rds start failed.' }
        Write-Host '  rds: start requested'
      }
    }

    $ec2State = Get-Ec2State $id
    if ($ec2State -eq 'running' -or $ec2State -eq 'pending') {
      Write-Host "  ec2: already $ec2State"
    } else {
      aws ec2 start-instances --instance-ids $id --region $Region --output text | Out-Null
      if ($LASTEXITCODE -ne 0) { Fail 'ec2 start failed.' }
      Write-Host '  ec2: start requested'
    }

    if (-not ([string]::IsNullOrWhiteSpace($rds) -or $rds -eq 'null')) {
      Write-Head 'waiting for rds (usually 3-8 min)'
      if (-not (Wait-Rds $rds 'available' 20)) {
        Write-Host 'WARN: rds did not reach "available" in 20 min - check the console.' -ForegroundColor Yellow
      }
    }

    # Bringing the containers back is NOT optional housekeeping.
    # On a daemon restart the compose stack comes back only PARTLY:
    #   kafka / postgres / qdrant   no restart policy -> stay DOWN
    #   spc-consumer / a-pred       on-failure:3      -> stay DOWN
    #   gateway / frontend / ...    unless-stopped    -> come UP
    # depends_on only orders "compose up"; it does not apply to daemon autostart.
    # So the screen loads while nothing flows - the exact failure shape we keep
    # getting bitten by. 'compose up -d' is idempotent, so we just always run it.
    if ($SkipCompose) {
      Write-Host ''
      Write-Host 'skipping "docker compose up -d" (-SkipCompose).' -ForegroundColor Yellow
      Write-Host 'WARNING: the stack comes back only partly on its own - screen up, no data.'
    } else {
      $script = @(
        'set -e',
        'cd /opt/aitch',
        'test -f docker-compose.yml || { echo "no docker-compose.yml in /opt/aitch - bundle missing"; exit 1; }',
        'docker compose up -d',
        'docker compose ps'
      )
      if (-not (Invoke-NodeScript $id $script 'bringing the stack up' 10)) {
        Write-Host 'compose up did not report success - check with -Ssm.' -ForegroundColor Yellow
      }
    }

    Write-Host ''
    Write-Host ("app url: {0}" -f (Get-Output 'app_url')) -ForegroundColor Green
    Write-Host 'The EIP is unchanged, so the URL is the same as before -Off.'
    exit 0
  }

  if ($Status) {
    Initialize-Terraform

    Write-Head 'terraform state'
    $resources = terraform state list 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $resources) {
      Write-Host 'state is empty - nothing is deployed (cost 0).' -ForegroundColor Green
      exit 0
    }
    Write-Host ("resources in state: {0}" -f ($resources | Measure-Object).Count)

    Write-Head 'outputs'
    terraform output

    $id = Get-Output 'app_instance_id'
    if (-not [string]::IsNullOrWhiteSpace($id)) {
      Write-Head 'live instance'
      $json = aws ec2 describe-instances --instance-ids $id --region $Region `
                --query 'Reservations[0].Instances[0].{state:State.Name,type:InstanceType,launched:LaunchTime,ip:PublicIpAddress}' `
                --output json 2>$null
      if ($LASTEXITCODE -eq 0 -and $json) {
        $inst = $json | ConvertFrom-Json
        Write-Host ("ec2      : {0}  ({1})" -f $inst.state, $inst.type)
        Write-Host ("public ip: {0}" -f $inst.ip)
        Write-Host ("launched : {0}" -f $inst.launched)

        $rds = Get-Output 'db_instance_id'
        $rdsState = 'not in state'
        if (-not ([string]::IsNullOrWhiteSpace($rds) -or $rds -eq 'null')) {
          $rdsState = Get-RdsState $rds
        }
        Write-Host ("rds      : {0}" -f $rdsState)

        Write-Head 'cost'
        if ($inst.state -eq 'running') {
          # LaunchTime resets on every start, so this reads "since last -On/-Up",
          # not lifetime spend. Enough to notice a stack left up overnight.
          $hours = ((Get-Date).ToUniversalTime() - [datetime]$inst.launched).TotalHours
          $spent = [math]::Round($hours * ($RateEc2 + $RateRds), 2)
          Write-Host ("up {0:N1} h since last start  ->  roughly `${1} of compute (rough rates)" -f $hours, $spent) -ForegroundColor Yellow
          Write-Host 'Short break:  .\scripts\app_stack.ps1 -Off    (keeps data, ~$8/mo)'
          Write-Host 'Done for good:.\scripts\app_stack.ps1 -Down   (cost 0, needs reseeding next time)'
        } elseif ($inst.state -eq 'stopped') {
          Write-Host 'compute is OFF - only storage bills (EBS 60GB + RDS 20GB + idle EIP ~ $8/mo).' -ForegroundColor Green
          Write-Host 'AWS auto-starts a stopped RDS after 7 days. Resume: -On  /  scrap: -Down'
        } else {
          Write-Host ("ec2 is '{0}' - transient state, re-run -Status in a minute." -f $inst.state)
        }
      }
    }
    exit 0
  }

  if ($Down) {
    Initialize-Terraform

    Write-Head 'terraform plan -destroy'
    terraform plan -destroy -input=false
    if ($LASTEXITCODE -ne 0) { Fail 'destroy plan failed.' }

    Confirm-Or-Exit 'destroy' @"
About to DESTROY the app stack in:
  $InfraDir

  RDS keeps a final snapshot unless db_skip_final_snapshot = true.
  The GPU node (srcgent_service\infra\llm-ec2-experiment) is a different state key and a
  different directory - it is NOT touched by this command.
"@

    Write-Head 'terraform destroy'
    terraform destroy -input=false -auto-approve
    if ($LASTEXITCODE -ne 0) { Fail 'destroy failed.' }

    Write-Host ''
    Write-Host 'destroyed - cost is back to 0 (final RDS snapshot may remain).' -ForegroundColor Green
    exit 0
  }

  # ---- pull a fresh code bundle onto a RUNNING node ------------------------
  # The boot script downloads the tar exactly once, at first boot. Re-uploading
  # to S3 changes nothing on a node that is already up, and nothing errors -
  # it just keeps running the old code. This is the verb for that gap.
  if ($Sync) {
    Initialize-Terraform
    $id = Get-InstanceId

    $bucket = ('var.bundle_s3_bucket' | terraform console 2>$null) -replace '"', ''
    $key    = ('var.bundle_s3_key'    | terraform console 2>$null) -replace '"', ''
    $bucket = $bucket.Trim(); $key = $key.Trim()
    if ([string]::IsNullOrWhiteSpace($key)) { Fail 'bundle_s3_key is empty - nothing to sync.' }

    $state = Get-Ec2State $id
    if ($state -ne 'running') { Fail "node is '$state' - start it first (-On)." }

    # SSM RunShellScript runs as root, so no sudo/group juggling here (an
    # interactive `-Ssm` session lands as ssm-user, which is a different story).
    $script = @(
      'set -e',
      "aws s3 cp s3://$bucket/$key /tmp/aitch-bundle.tar.gz --region $Region",
      'tar -xzf /tmp/aitch-bundle.tar.gz -C /opt/aitch',
      'chown -R ec2-user:ec2-user /opt/aitch',
      'rm -f /tmp/aitch-bundle.tar.gz',
      'cat /opt/aitch/BUNDLE_INFO.txt'
    )
    if (-not (Invoke-NodeScript $id $script 'pulling bundle onto the node' 5)) {
      Fail 'bundle sync failed.'
    }

    Write-Host ''
    Write-Host 'bundle unpacked.' -ForegroundColor Green

    # Containers mount the source, but the processes inside them do not reload
    # it - without a restart you get "I fixed it and nothing changed".
    if ($SkipCompose) {
      Write-Host 'skipping "docker compose restart" (-SkipCompose) - running processes keep the OLD code.' -ForegroundColor Yellow
    } else {
      $restart = @(
        'set -e',
        'cd /opt/aitch',
        'docker compose restart',
        'docker compose ps'
      )
      if (-not (Invoke-NodeScript $id $restart 'restarting containers' 10)) {
        Write-Host 'restart did not report success - check with -Ssm.' -ForegroundColor Yellow
      }
    }

    Write-Host ''
    Write-Host 'NOTE: tar overlays - files DELETED in git stay on the node. For a clean tree,'
    Write-Host '      replace the instance instead:  terraform apply -replace=aws_instance.app'
    Write-Host '      (that also re-downloads hf_cache 4.7GB, so prefer -Sync for code edits.)'
    exit 0
  }

  if ($Ssm) {
    $id = Get-InstanceId
    Write-Host "opening SSM session to $id ..."
    aws ssm start-session --target $id --region $Region
    exit 0
  }

  if ($Log) {
    $id = Get-InstanceId
    Write-Host "tailing /var/log/aitch-bootstrap.log on $id (Ctrl+C to stop) ..."
    Write-Host 'If this returns nothing, boot has not reached the logging stage yet.'
    aws ssm start-session --target $id --region $Region `
      --document-name AWS-StartInteractiveCommand `
      --parameters 'command=["sudo tail -n 200 -f /var/log/aitch-bootstrap.log"]'
    exit 0
  }

} finally {
  Pop-Location
}
