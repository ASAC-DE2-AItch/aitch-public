# ============================================================================
# llm_stack.ps1 - AITCH GPU node (vLLM) lifecycle driver for infra\llm-stack.
#
# Sibling of scripts\app_stack.ps1. Three stacks, three state keys:
#
#   state key                            applier   dir
#   -------------------------------------------------------------------------
#   tfstate/app-stack.tfstate            C only    infra\app-stack
#   tfstate/llm-stack.tfstate            C only    infra\llm-stack          <-- this
#   tfstate/llm-ec2-experiment.tfstate   PM only   src\agent_service\infra\llm-ec2-experiment
#                                                  ^ LEGACY - still owns the live node
#
# WHY A NEW STACK: the legacy one runs in the DEFAULT VPC, so the app has to
# reach vLLM over a PUBLIC IP that changes on every restart (two full-fallback
# incidents on 8/5, and the reason sync_llm_endpoint.ps1 existed). This stack
# joins the app VPC, so the app talks to a PRIVATE IP that survives stop/start -
# the "refresh the endpoint" step disappears from the deploy path entirely.
#
# CUTOVER ORDER - do not skip step 1 or you pay for two GPUs:
#   1. .\scripts\llm_power.ps1 -Down      (legacy dir: destroy the old node)
#   2. .\scripts\llm_stack.ps1  -Up       (this stack, ~20 min)
#   3. delete the legacy dir + llm_power.ps1, update references
# The terraform config also refuses to apply while the legacy node exists.
#
# Usage (from repo root):
#   .\scripts\llm_stack.ps1 -Plan      # init + validate + plan  (no cost, no state change)
#   .\scripts\llm_stack.ps1 -Up        # apply                   (COST STARTS HERE, ~20 min to serve)
#   .\scripts\llm_stack.ps1 -Off       # stop     (EBS only, ~$18/mo)
#   .\scripts\llm_stack.ps1 -On        # start    (3-8 min to serve; private IP unchanged)
#   .\scripts\llm_stack.ps1 -Status    # power + serving probe + cost so far
#   .\scripts\llm_stack.ps1 -Down      # destroy  (cost 0; weights come back from S3)
#   .\scripts\llm_stack.ps1 -Ssm       # session into the node
#   .\scripts\llm_stack.ps1 -Log       # tail /var/log/llm-bootstrap.log
#
#   -Yes           skip the typed confirmation on -Up / -Down
#   -MaxWaitMin N  how long -Up / -On wait for vLLM to answer (default 35 / 15)
#
# A GPU left idle is the single most expensive mistake here (~$1.86/h). -Status
# says how long it has been up and what that costs.
# ============================================================================

param(
  [switch]$Plan,
  [switch]$Up,
  [switch]$On,
  [switch]$Off,
  [switch]$Down,
  [switch]$Status,
  [switch]$Ssm,
  [switch]$Log,
  [switch]$Yes,
  [int]$MaxWaitMin = 0
)

$ErrorActionPreference = 'Continue'

$Region   = 'ap-northeast-2'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$InfraDir = Join-Path $RepoRoot 'infra\llm-stack'
$TfVars   = Join-Path $InfraDir 'terraform.tfvars'
$LegacyTag = 'aitch-llm-lab'

# g6e.xlarge on-demand, ap-northeast-2. Rough - for the idle warning only.
$RateGpu = 1.861

function Write-Head($text) {
  Write-Host ''
  Write-Host "=== $text ===" -ForegroundColor Cyan
}

function Fail($text) {
  Write-Host "ERROR: $text" -ForegroundColor Red
  exit 1
}

$picked = @()
foreach ($m in @('Plan', 'Up', 'On', 'Off', 'Down', 'Status', 'Ssm', 'Log')) {
  if (Get-Variable -Name $m -ValueOnly) { $picked += $m }
}
if ($picked.Count -ne 1) {
  Write-Host 'Pick exactly one: -Plan | -Up | -Off | -On | -Status | -Down | -Ssm | -Log'
  exit 1
}

if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) { Fail 'terraform not found in PATH.' }
if (-not (Get-Command aws -ErrorAction SilentlyContinue))       { Fail 'aws cli not found in PATH.' }
if (-not (Test-Path $InfraDir))                                 { Fail "infra dir not found: $InfraDir" }

# Same belt-and-braces as app_stack.ps1: the separate directory IS the guard
# against a destroy reaching the wrong stack, so refuse if the path drifted.
if ((Split-Path -Leaf $InfraDir) -ne 'llm-stack') {
  Fail "refusing to run: resolved dir is not llm-stack ($InfraDir)"
}

if ($MaxWaitMin -le 0) { if ($Up) { $MaxWaitMin = 35 } else { $MaxWaitMin = 15 } }

Push-Location $InfraDir
try {

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
    Write-Host 'It needs three values from the app stack (no defaults on purpose):'
    Write-Host '    cd infra\app-stack'
    Write-Host '    terraform output -raw vpc_id'
    Write-Host '    terraform output -raw app_subnet_id'
    Write-Host '    terraform output -raw llm_security_group_id'
    Write-Host 'Then: copy terraform.tfvars.example terraform.tfvars'
    Fail 'terraform.tfvars required.'
  }

  # The config has a precondition for this too, but failing here is friendlier:
  # we can say exactly which command clears it.
  function Assert-NoLegacyNode {
    $ids = aws ec2 describe-instances --region $Region `
             --filters "Name=tag:Name,Values=$LegacyTag" `
             --query 'Reservations[].Instances[?State.Name!=`terminated`].InstanceId' `
             --output text 2>$null
    if ([string]::IsNullOrWhiteSpace($ids)) { return }
    Write-Host ''
    Write-Host "legacy GPU node still exists (tag $LegacyTag): $ids" -ForegroundColor Red
    Write-Host 'Applying now would leave TWO GPU nodes running - that does not raise an error,'
    Write-Host 'it just doubles the bill (~$1.86/h each) until somebody notices.'
    Write-Host ''
    Write-Host 'Clear it first:   .\scripts\llm_power.ps1 -Down'
    Fail 'refusing to apply while the legacy node exists.'
  }

  function Get-Output($name) {
    $v = terraform output -raw $name 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $v
  }

  function Get-InstanceId {
    $id = Get-Output 'instance_id'
    if ([string]::IsNullOrWhiteSpace($id)) {
      Fail 'no instance_id in state - is the stack up? (.\scripts\llm_stack.ps1 -Status)'
    }
    return $id
  }

  function Get-Ec2State($id) {
    $s = aws ec2 describe-instances --instance-ids $id --region $Region `
           --query 'Reservations[0].Instances[0].State.Name' --output text 2>$null
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    return $s
  }

  # "running" is not "serving": the box boots, restores ~20GB of weights from S3
  # and only then loads the GPU. Poll the OpenAI endpoint, not the instance state.
  function Wait-Serving($ip, $port, $minutes) {
    $deadline = (Get-Date).AddMinutes($minutes)
    Write-Host "waiting for vLLM on http://${ip}:${port} (up to $minutes min) ..."
    while ((Get-Date) -lt $deadline) {
      try {
        $r = Invoke-RestMethod "http://${ip}:${port}/v1/models" -TimeoutSec 5
        if ($r) {
          $len = $r.data[0].max_model_len
          Write-Host "READY - max_model_len=$len" -ForegroundColor Green
          return $len
        }
      } catch { }
      Start-Sleep -Seconds 20
      Write-Host '  ...'
    }
    Write-Host 'not serving yet - check .\scripts\llm_stack.ps1 -Log' -ForegroundColor Yellow
    return $null
  }

  # ---- modes ---------------------------------------------------------------
  if ($Plan) {
    Assert-TfVars
    Initialize-Terraform
    Write-Head 'terraform validate'
    terraform validate
    if ($LASTEXITCODE -ne 0) { Fail 'validate failed.' }
    Write-Head 'terraform plan'
    terraform plan -input=false -out=tfplan
    if ($LASTEXITCODE -ne 0) { Fail 'plan failed.' }
    Write-Host ''
    Write-Host 'plan only - nothing was created.' -ForegroundColor Green
    exit 0
  }

  if ($Up) {
    Assert-TfVars
    Initialize-Terraform
    Assert-NoLegacyNode

    Write-Head 'terraform plan'
    terraform plan -input=false -out=tfplan
    if ($LASTEXITCODE -ne 0) { Fail 'plan failed - not applying.' }

    if (-not $Yes) {
      Write-Host ''
      Write-Host ("About to CREATE the GPU node (~`$$RateGpu/h while running).") -ForegroundColor Yellow
      Write-Host 'Serving is ready ~20 min after apply returns (S3 weight restore + GPU load).'
      Write-Host 'A GPU left idle overnight is the most expensive mistake here - use -Off.'
      $a = Read-Host 'Type apply to continue'
      if ($a -ne 'apply') { Write-Host 'aborted.'; exit 1 }
    }

    Write-Head 'terraform apply'
    terraform apply -input=false tfplan
    if ($LASTEXITCODE -ne 0) { Fail 'apply failed.' }

    Write-Head 'outputs'
    terraform output

    $pub  = Get-Output 'public_ip'
    $priv = Get-Output 'private_ip'
    $len  = Wait-Serving $pub 8000 $MaxWaitMin

    Write-Host ''
    Write-Host 'Next - the app node has to be told where vLLM is:' -ForegroundColor Cyan
    Write-Host ("  AGENT_LLM_BASE_URL      = http://{0}:8000" -f $priv)
    if ($len) { Write-Host ("  AGENT_LLM_MAX_MODEL_LEN = {0}   <- server's actual value" -f $len) }
    Write-Host '  (edit /opt/aitch/.env ON THE APP NODE, not the laptop .env)'
    Write-Host '  .\scripts\app_stack.ps1 -Ssm   ->   vi /opt/aitch/.env && docker compose restart'
    Write-Host ''
    Write-Host 'The private IP survives stop/start, so this is a one-time edit.' -ForegroundColor Green
    exit 0
  }

  if ($Off) {
    Initialize-Terraform
    $id = Get-InstanceId
    $st = Get-Ec2State $id
    if ($st -eq 'stopped' -or $st -eq 'stopping') {
      Write-Host "already $st"
    } else {
      aws ec2 stop-instances --instance-ids $id --region $Region --output text | Out-Null
      if ($LASTEXITCODE -ne 0) { Fail 'stop failed.' }
      Write-Host 'stop requested' -ForegroundColor Green
    }
    Write-Host ''
    Write-Host 'GPU billing stops once it reaches stopped. Root EBS 200GB gp3 stays (~$18/mo).'
    Write-Host 'Private IP is preserved, so the app .env stays valid across -Off/-On.'
    Write-Host 'Done with the experiment entirely? .\scripts\llm_stack.ps1 -Down   (cost 0)'
    exit 0
  }

  if ($On) {
    Initialize-Terraform
    $id = Get-InstanceId
    $st = Get-Ec2State $id
    if ($st -eq 'running' -or $st -eq 'pending') {
      Write-Host "already $st"
    } else {
      aws ec2 start-instances --instance-ids $id --region $Region --output text | Out-Null
      if ($LASTEXITCODE -ne 0) { Fail 'start failed.' }
      Write-Host 'start requested'
    }
    $priv = Get-Output 'private_ip'
    $pub  = aws ec2 describe-instances --instance-ids $id --region $Region `
              --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>$null
    Write-Host ''
    Write-Host "private ip: $priv  (unchanged - the app .env does NOT need editing)" -ForegroundColor Green
    Wait-Serving $pub 8000 $MaxWaitMin | Out-Null
    exit 0
  }

  if ($Status) {
    Initialize-Terraform
    $res = terraform state list 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $res) {
      Write-Host 'state is empty - this stack is not deployed (cost 0).' -ForegroundColor Green
      Write-Host 'The legacy stack may still own a node - check .\scripts\llm_power.ps1 -Status'
      exit 0
    }
    $id = Get-InstanceId
    $json = aws ec2 describe-instances --instance-ids $id --region $Region `
              --query 'Reservations[0].Instances[0].{state:State.Name,type:InstanceType,launched:LaunchTime,pub:PublicIpAddress,priv:PrivateIpAddress}' `
              --output json 2>$null
    $i = $json | ConvertFrom-Json
    Write-Head 'gpu node'
    Write-Host ("state      : {0}  ({1})" -f $i.state, $i.type)
    Write-Host ("private ip : {0}   <- app uses this" -f $i.priv)
    Write-Host ("public ip  : {0}   <- eval rounds from the laptop only" -f $i.pub)

    if ($i.state -eq 'running') {
      $h = ((Get-Date).ToUniversalTime() - [datetime]$i.launched).TotalHours
      Write-Head 'cost'
      Write-Host ("up {0:N1} h since last start -> roughly `${1} (rough rate)" -f $h, [math]::Round($h * $RateGpu, 2)) -ForegroundColor Yellow
      Write-Host 'Not measuring right now? .\scripts\llm_stack.ps1 -Off'
      Write-Head 'serving probe'
      try {
        $r = Invoke-RestMethod ("http://{0}:8000/v1/models" -f $i.pub) -TimeoutSec 5
        Write-Host ("READY - max_model_len={0}" -f $r.data[0].max_model_len) -ForegroundColor Green
      } catch {
        Write-Host 'not answering yet (boot + GPU load takes 3-8 min after start).' -ForegroundColor Yellow
      }
    } elseif ($i.state -eq 'stopped') {
      Write-Host ''
      Write-Host 'GPU billing is OFF - only the root EBS remains (~$18/mo).' -ForegroundColor Green
    }
    exit 0
  }

  if ($Down) {
    Initialize-Terraform
    Write-Head 'terraform plan -destroy'
    terraform plan -destroy -input=false
    if ($LASTEXITCODE -ne 0) { Fail 'destroy plan failed.' }
    if (-not $Yes) {
      Write-Host ''
      Write-Host 'About to DESTROY the GPU node.' -ForegroundColor Yellow
      Write-Host 'Nothing is lost - model weights live in S3 and boot restores them.'
      Write-Host 'Recreating takes ~20 min, so prefer -Off for short breaks.'
      $a = Read-Host 'Type destroy to continue'
      if ($a -ne 'destroy') { Write-Host 'aborted.'; exit 1 }
    }
    terraform destroy -input=false -auto-approve
    if ($LASTEXITCODE -ne 0) { Fail 'destroy failed.' }
    Write-Host ''
    Write-Host 'destroyed - cost 0.' -ForegroundColor Green
    Write-Host 'The app .env now points at a dead address - update it on the next -Up.'
    exit 0
  }

  if ($Ssm) {
    aws ssm start-session --target (Get-InstanceId) --region $Region
    exit 0
  }

  if ($Log) {
    $id = Get-InstanceId
    Write-Host "tailing /var/log/llm-bootstrap.log on $id (Ctrl+C to stop) ..."
    aws ssm start-session --target $id --region $Region `
      --document-name AWS-StartInteractiveCommand `
      --parameters 'command=["sudo tail -n 200 -f /var/log/llm-bootstrap.log"]'
    exit 0
  }

} finally {
  Pop-Location
}
