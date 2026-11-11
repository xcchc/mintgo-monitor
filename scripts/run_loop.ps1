<#
Continuous loop runner for mintgo-monitor.

Use this when you do NOT want a Windows scheduled task (e.g. running inside a
terminal you keep open, or on a machine where task registration is blocked).

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\run_loop.ps1
  powershell -ExecutionPolicy Bypass -File scripts\run_loop.ps1 -IntervalMinutes 15

Stop with Ctrl+C.

Keep this file ASCII-only: Windows PowerShell 5.1 reads .ps1 as ANSI/GBK by
default, so non-ASCII text breaks parsing unless the file has a BOM.
#>
param(
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
$py = Join-Path $projectRoot "src\run_once.py"
$logDir = Join-Path $projectRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$loopLog = Join-Path $logDir "loop.log"

function Write-LoopLog([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $loopLog -Value $line -Encoding UTF8
}

Write-LoopLog "loop started, interval = $IntervalMinutes min"
Write-LoopLog "stop with Ctrl+C"

$round = 0
while ($true) {
    $round++
    Write-LoopLog "round $round start"
    $env:PYTHONIOENCODING = "utf-8"
    Push-Location $projectRoot
    try {
        & python -X utf8 $py
        $code = $LASTEXITCODE
    } catch {
        $code = -1
        Write-LoopLog ("round {0} threw: {1}" -f $round, $_.Exception.Message)
    } finally {
        Pop-Location
    }
    Write-LoopLog "round $round done, exit=$code"

    $next = (Get-Date).AddMinutes($IntervalMinutes)
    Write-LoopLog ("sleeping until {0:HH:mm:ss}" -f $next)
    Start-Sleep -Seconds ($IntervalMinutes * 60)
}
