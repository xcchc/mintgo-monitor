<#
Register the mintgo-monitor scheduled task.

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -IntervalMinutes 15
  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove

Notes:
  - Repeats every N minutes; MultipleInstances=IgnoreNew prevents overlap.
  - Runs interactively as the current user (needs the user's Chrome/Playwright env).
  - Logs append to logs\task.log; reports land in data\reports\.
  - Keep this file ASCII-only: Windows PowerShell 5.1 reads .ps1 as ANSI/GBK by
    default, so non-ASCII text breaks parsing unless the file has a BOM.
#>
param(
    [int]$IntervalMinutes = 30,
    [string]$TaskName = "mintgo-monitor",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $projectRoot "scripts\run.bat"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Yellow
    } else {
        Write-Host "Task not found: $TaskName" -ForegroundColor DarkGray
    }
    return
}

if (-not (Test-Path $bat)) {
    throw "Runner not found: $bat"
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed old task, rebuilding" -ForegroundColor DarkGray
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $projectRoot

$startAt = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $startAt -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 2)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Periodically fetch mintgo.fun NFT mint data and produce a screening report" | Out-Null

Write-Host ""
Write-Host "[OK] Registered scheduled task: $TaskName" -ForegroundColor Green
Write-Host "     interval : every $IntervalMinutes min"
Write-Host "     first run: $($startAt.ToString('HH:mm:ss'))"
Write-Host "     runner   : $bat"
Write-Host "     log      : $projectRoot\logs\task.log"
Write-Host "     reports  : $projectRoot\data\reports\"
Write-Host ""
Write-Host "Check  : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "Run now: Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove : powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove"
