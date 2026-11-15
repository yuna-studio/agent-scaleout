# agent-scaleout node setup (Windows). Normally run for you by the `scaleout invite NAME --os windows` one-liner.
#   setup.ps1 -Name build-win [-Dir ~\scaleout-work] [-MaxJobs 2] [-Agent claude|codex|gemini|"<command>"]
#   setup.ps1 -Repair | -Uninstall
# Needs: Windows 10 1809+ / 11 (OpenSSH, tar.exe built in). Reverse tunnels are not supported on Windows nodes yet:
# put them on the orchestrator's LAN. UNTESTED on real hardware — please report issues.
param([string]$Name = "", [string]$Dir = "~\scaleout-work", [int]$MaxJobs = 2, [string]$Agent = "claude", [switch]$Repair, [switch]$Uninstall)
$ErrorActionPreference = "Stop"
$B = Join-Path $env:USERPROFILE ".scaleout"; $Pkg = Split-Path -Parent $PSCommandPath
$Task = "scaleout-runner"

function Register-Runner {
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$B\bin\runner.ps1`""
  $t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 0)
  Register-ScheduledTask -TaskName $Task -Action $action -Trigger $t1, $t2 -Settings $set -Force | Out-Null
  "SCALEOUT_RUNNER schtasks"
}

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
  Remove-Item "$B\bin", "$B\queue" -Recurse -Force -ErrorAction SilentlyContinue
  "SCALEOUT_UNINSTALLED"; exit 0
}
if ($Repair) { Register-Runner; exit 0 }
if (-not $Name) { throw "-Name is required" }

New-Item -ItemType Directory -Force "$B\bin", "$B\queue", "$B\jobs", "$B\work", "$B\settings" | Out-Null
foreach ($f in "ctl.ps1", "runner.ps1", "setup.ps1") { Copy-Item (Join-Path $Pkg $f) "$B\bin\$f" -Force }
if (-not (Test-Path "$B\settings\claude.json") -and (Test-Path (Join-Path $Pkg "claude-settings.json"))) { Copy-Item (Join-Path $Pkg "claude-settings.json") "$B\settings\claude.json" }
$agentName = $Agent; $agentCmd = ""
if ($Agent -notin @("claude", "codex", "gemini")) { $agentName = "custom"; $agentCmd = $Agent }
@{ NAME = $Name; WORK_ROOT = $Dir; MAX_JOBS = $MaxJobs; AGENT = $agentName; AGENT_CMD = $agentCmd; MIN_FREE_GB = 2 } | ConvertTo-Json | Set-Content "$B\node.json"
Register-Runner

$hk = Get-Content "$env:ProgramData\ssh\ssh_host_ed25519_key.pub" -ErrorAction SilentlyContinue
$lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1).IPAddress
$agents = @(); foreach ($x in "claude", "codex", "gemini") { if (Get-Command $x -ErrorAction SilentlyContinue) { $agents += "`"$x`"" } }
"SCALEOUT_OS Windows"
"SCALEOUT_USER $env:USERNAME"
if ($hk) { "SCALEOUT_HOSTKEY $(($hk -split ' ')[0..1] -join ' ')" }
"SCALEOUT_LAN $lan"
"SCALEOUT_AGENTS [$($agents -join ',')]"
