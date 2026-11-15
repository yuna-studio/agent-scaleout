# agent-scaleout node runner (Windows). Started by the "scaleout-runner" scheduled task (at logon, every minute,
# and on demand by ctl.ps1 submit). Runs queued jobs with the configured agent, writes heartbeat/rc/out.
# UNTESTED on real hardware — please report issues.
$ErrorActionPreference = "Continue"
$B = Join-Path $env:USERPROFILE ".scaleout"; $Q = Join-Path $B "queue"; $J = Join-Path $B "jobs"
$Conf = @{ MAX_JOBS = 2; AGENT = "claude"; AGENT_CMD = ""; MIN_FREE_GB = 2 }
$cf = Join-Path $B "node.json"
if (Test-Path $cf) { (Get-Content $cf -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $Conf[$_.Name] = $_.Value } }
New-Item -ItemType Directory -Force $Q, $J, (Join-Path $B "work") | Out-Null

# single instance
$mutex = New-Object Threading.Mutex($false, "Global\scaleout-runner-$env:USERNAME")
if (-not $mutex.WaitOne(0)) { exit 0 }

function Expand-Home([string]$p) { if ($p.StartsWith("~")) { return Join-Path $env:USERPROFILE $p.Substring(1).TrimStart("\", "/") } return $p }
function Free-GB { [int]([math]::Floor((Get-Item $env:USERPROFILE).PSDrive.Free / 1GB)) }

function Start-Job1([string]$d) {
  $meta = Get-Content "$d\meta.json" -Raw | ConvertFrom-Json
  $agent = $(if ($meta.AGENT -and $meta.AGENT -ne "default") { $meta.AGENT } else { $Conf.AGENT })
  $wd = Expand-Home $(if ($meta.WORKDIR) { $meta.WORKDIR } else { "~" }); New-Item -ItemType Directory -Force $wd | Out-Null
  if ((Free-GB) -lt $Conf.MIN_FREE_GB) { "node disk almost full" | Set-Content "$d\err.log"; "28" | Set-Content "$d\rc"; return $null }
  $settings = Join-Path $B "settings\claude.json"
  switch ($agent) {
    "claude" { $exe = "claude"; $argline = "-p --output-format json" + $(if (Test-Path $settings) { " --settings `"$settings`"" } else { "" }) }
    "codex"  { $exe = "codex"; $argline = "exec --skip-git-repo-check -" }
    "gemini" { $exe = "gemini"; $argline = "-p -" }
    default  { $exe = "powershell"; $argline = "-NoProfile -Command `"$(if ($Conf.AGENT_CMD) { $Conf.AGENT_CMD } else { $agent })`"" }
  }
  $cmd = (Get-Command $exe -ErrorAction SilentlyContinue).Source; if (-not $cmd) { $cmd = $exe }
  [int][double]::Parse((Get-Date -UFormat %s)) | Set-Content "$d\started"; New-Item -ItemType File -Force "$d\heartbeat" | Out-Null
  $p = Start-Process -FilePath $cmd -ArgumentList $argline -WorkingDirectory $wd -NoNewWindow -PassThru `
       -RedirectStandardInput "$d\prompt.md" -RedirectStandardOutput "$d\out" -RedirectStandardError "$d\err.log"
  $p.Id | Set-Content "$d\pid"
  return @{ Proc = $p; Dir = $d; Deadline = (Get-Date).AddMinutes([int]$meta.TIMEOUT_MIN) }
}

$active = @()
while ($true) {
  foreach ($a in @($active)) {
    if ($a.Proc.HasExited) {
      $rc = $(if (Test-Path "$($a.Dir)\cancelled") { 130 } else { $a.Proc.ExitCode })
      "$rc" | Set-Content "$($a.Dir)\rc"; [int][double]::Parse((Get-Date -UFormat %s)) | Set-Content "$($a.Dir)\finished"
      $active = @($active | Where-Object { $_ -ne $a })
    } else {
      (Get-Item "$($a.Dir)\heartbeat").LastWriteTime = Get-Date
      if ((Get-Date) -gt $a.Deadline) { "timed out" | Add-Content "$($a.Dir)\err.log"; Stop-Process -Id $a.Proc.Id -Force -ErrorAction SilentlyContinue }
    }
  }
  $queued = @(Get-ChildItem $Q -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime)
  if ($queued.Count -eq 0 -and $active.Count -eq 0) { break }
  foreach ($f in $queued) {
    if ($active.Count -ge [int]$Conf.MAX_JOBS) { break }
    Remove-Item $f.FullName -Force; $d = Join-Path $J $f.Name
    if (Test-Path $d) { $s = Start-Job1 $d; if ($s) { $active += $s } }
  }
  Start-Sleep -Seconds 10
}
$mutex.ReleaseMutex()
