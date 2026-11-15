# agent-scaleout node control (Windows, PowerShell 5.1+). The orchestrator calls this over SSH:
#   powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\.scaleout\bin\ctl.ps1 <verb> [args]
# Same verbs and outputs as node/unix/ctl.sh. UNTESTED on real hardware — please report issues.
param([Parameter(Position = 0)][string]$Verb = "", [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest = @())
$ErrorActionPreference = "Stop"
$B = Join-Path $env:USERPROFILE ".scaleout"
$Version = "__VERSION__"
$Conf = @{ NAME = ""; WORK_ROOT = "~\scaleout-work"; MAX_JOBS = 2; AGENT = "claude"; AGENT_CMD = ""; MIN_FREE_GB = 2 }
$cf = Join-Path $B "node.json"
if (Test-Path $cf) { (Get-Content $cf -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $Conf[$_.Name] = $_.Value } }

function Expand-Home([string]$p) { if ($p.StartsWith("~")) { return Join-Path $env:USERPROFILE $p.Substring(1).TrimStart("\", "/") } return $p }
function Disk-FreeGB([string]$p = $env:USERPROFILE) { $d = (Get-Item (Expand-Home $p)).PSDrive; return [int]([math]::Floor($d.Free / 1GB)) }
function Mem-FreePct { $o = Get-CimInstance Win32_OperatingSystem; return [int]($o.FreePhysicalMemory * 100 / $o.TotalVisibleMemorySize) }
function Mem-TotalGB { $o = Get-CimInstance Win32_OperatingSystem; return [int]($o.TotalVisibleMemorySize / 1MB) }
function Running-Jobs { @(Get-ChildItem (Join-Path $B "jobs") -Directory -ErrorAction SilentlyContinue | Where-Object { (Test-Path "$($_.FullName)\started") -and -not (Test-Path "$($_.FullName)\rc") }).Count }
function Queued-Jobs { @(Get-ChildItem (Join-Path $B "queue") -File -ErrorAction SilentlyContinue).Count }
function Agents { $a = @(); foreach ($x in "claude", "codex", "gemini") { if (Get-Command $x -ErrorAction SilentlyContinue) { $a += $x } }; if ($Conf.AGENT_CMD) { $a += "custom" }; return , $a }
function Devices { $a = 0; if (Get-Command adb -ErrorAction SilentlyContinue) { $a = @(adb devices | Select-Object -Skip 1 | Where-Object { $_ -match "\tdevice$" }).Count }; return @{ android = $a; ios = 0 } }
function Runner-Ok { return [bool](Get-ScheduledTask -TaskName "scaleout-runner" -ErrorAction SilentlyContinue) }
function Read-StdinBytes([string]$to) { $in = [Console]::OpenStandardInput(); $fs = [IO.File]::Create($to); $in.CopyTo($fs); $fs.Close() }
function Kick { Start-ScheduledTask -TaskName "scaleout-runner" -ErrorAction SilentlyContinue }

switch ($Verb) {
  "version" { $Version }
  "metrics" {
    $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $n = [Environment]::ProcessorCount
    [ordered]@{ os = "Windows"; arch = $env:PROCESSOR_ARCHITECTURE; host = $env:COMPUTERNAME; mem_free_pct = (Mem-FreePct); mem_total_gb = (Mem-TotalGB)
      disk_free_gb = (Disk-FreeGB); work_free_gb = (Disk-FreeGB $Conf.WORK_ROOT); load = [math]::Round($cpu / 100 * $n, 2); cpus = $n
      running = (Running-Jobs); queued = (Queued-Jobs); runner = (Runner-Ok); agents = (Agents); devices = (Devices)
      skills_hash = $(if (Test-Path "$B\skills.hash") { (Get-Content "$B\skills.hash" -Raw).Trim() } else { "none" }); version = $Version } | ConvertTo-Json -Compress
  }
  "submit" {
    $jid, $agent, $timeout, $wd = $Rest
    $d = Join-Path $B "jobs\$jid"; New-Item -ItemType Directory -Force $d, (Join-Path $B "queue") | Out-Null
    $prompt = [Console]::In.ReadToEnd(); [IO.File]::WriteAllText("$d\prompt.md", $prompt)
    @{ AGENT = $agent; TIMEOUT_MIN = [int]$timeout; WORKDIR = $wd } | ConvertTo-Json | Set-Content "$d\meta.json"
    New-Item -ItemType File -Force (Join-Path $B "queue\$jid") | Out-Null
    Kick; "queued"
  }
  "state" {
    $d = Join-Path $B "jobs\$($Rest[0])"
    if (-not (Test-Path $d)) { '{"state":"missing"}'; break }
    if (Test-Path "$d\rc") { '{"state":"done","rc":' + ((Get-Content "$d\rc" -Raw).Trim()) + '}' }
    elseif (Test-Path "$d\started") { $age = [int]((Get-Date) - (Get-Item "$d\heartbeat").LastWriteTime).TotalSeconds; '{"state":"running","heartbeat_age":' + $age + '}' }
    elseif (Test-Path (Join-Path $B "queue\$($Rest[0])")) { '{"state":"queued"}' } else { '{"state":"lost"}' }
  }
  "result" { $f = Join-Path $B "jobs\$($Rest[0])\out"; if (Test-Path $f) { [Console]::Out.Write([IO.File]::ReadAllText($f)) } }
  "err" { $f = Join-Path $B "jobs\$($Rest[0])\err.log"; if (Test-Path $f) { Get-Content $f -Tail $(if ($Rest.Count -gt 1) { [int]$Rest[1] } else { 40 }) } }
  "cancel" {
    $jid = $Rest[0]; $d = Join-Path $B "jobs\$jid"
    Remove-Item (Join-Path $B "queue\$jid") -Force -ErrorAction SilentlyContinue
    if (Test-Path $d) { New-Item -ItemType File -Force "$d\cancelled" | Out-Null
      if (Test-Path "$d\pid") { Stop-Process -Id ([int](Get-Content "$d\pid")) -Force -ErrorAction SilentlyContinue }
      if (-not (Test-Path "$d\rc") -and -not (Test-Path "$d\started")) { "130" | Set-Content "$d\rc" } }
    "cancelled"
  }
  "rm" { foreach ($p in "jobs", "queue", "work") { Remove-Item (Join-Path $B "$p\$($Rest[0])") -Recurse -Force -ErrorAction SilentlyContinue }; "removed" }
  "gc" {
    $days = $(if ($Rest.Count) { [int]$Rest[0] } else { 3 }); $cut = (Get-Date).AddDays(-$days); $n = 0
    Get-ChildItem (Join-Path $B "jobs") -Directory -ErrorAction SilentlyContinue | Where-Object { (Test-Path "$($_.FullName)\rc") -and (Get-Item "$($_.FullName)\rc").LastWriteTime -lt $cut } | ForEach-Object { Remove-Item $_.FullName -Recurse -Force; $n++ }
    Get-ChildItem (Join-Path $B "work") -Directory -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt $cut } | Remove-Item -Recurse -Force
    "removed $n"
  }
  "cleanup" {
    $before = Disk-FreeGB
    & $PSCommandPath gc 1 | Out-Null
    foreach ($p in "$env:LOCALAPPDATA\pip\Cache", "$env:LOCALAPPDATA\npm-cache\_cacache", "$env:TEMP\*") { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }
    if ($Rest -contains "aggressive" -or $Rest -contains "deep") { foreach ($p in "$env:USERPROFILE\.gradle\caches", "$env:LOCALAPPDATA\Yarn\Cache") { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue } }
    if ($Rest -contains "deep" -and (Get-Command docker -ErrorAction SilentlyContinue)) { docker system prune -af | Out-Null }
    "disk_free_gb $before -> $(Disk-FreeGB)"
  }
  "recv" { $t = Expand-Home $Rest[0]; New-Item -ItemType Directory -Force $t | Out-Null; $tmp = [IO.Path]::GetTempFileName(); Read-StdinBytes $tmp; tar -xf $tmp -C $t; Remove-Item $tmp; "ok" }
  "send" { $t = Expand-Home $Rest[0]; & tar.exe -cf - -C $t --exclude node_modules --exclude .gradle --exclude build --exclude .dart_tool . }
  "skills-hash" { $Rest[0] | Set-Content "$B\skills.hash"; "ok" }
  "repair" { & powershell -NoProfile -ExecutionPolicy Bypass -File "$B\bin\setup.ps1" -Repair; "repaired" }
  "update" { $tmp = [IO.Path]::GetTempFileName(); Read-StdinBytes $tmp; $p = "$B\pkg"; New-Item -ItemType Directory -Force $p | Out-Null; tar -xzf $tmp -C $p; Remove-Item $tmp
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$p\setup.ps1" -Name $Conf.NAME -Dir $Conf.WORK_ROOT -MaxJobs $Conf.MAX_JOBS -Agent $Conf.AGENT }
  "doctor" {
    "host: $env:COMPUTERNAME - Windows $([Environment]::OSVersion.Version) - $Version"
    "memory: $(Mem-TotalGB)GB, free $(Mem-FreePct)% - disk free $(Disk-FreeGB)GB"
    foreach ($t in "claude", "codex", "gemini", "git", "gh", "node", "npm", "python", "flutter", "docker") {
      $c = Get-Command $t -ErrorAction SilentlyContinue; "  {0,-10} {1}" -f $t, $(if ($c) { (& $t --version 2>$null | Select-Object -First 1) } else { "-" }) }
    "runner: $(Runner-Ok) - agent: $($Conf.AGENT)"
  }
  "jobs" { Get-ChildItem (Join-Path $B "jobs") -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 20 | ForEach-Object {
      $s = $(if (Test-Path "$($_.FullName)\rc") { "done rc=$((Get-Content "$($_.FullName)\rc" -Raw).Trim())" } elseif (Test-Path "$($_.FullName)\started") { "running" } else { "queued" }); "$($_.Name) $s" } }
  default { [Console]::Error.WriteLine("unknown verb: $Verb"); exit 4 }
}
