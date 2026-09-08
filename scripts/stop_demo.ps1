$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir = Split-Path -Parent $ScriptDir
$BackendDir = Join-Path $BaseDir "backend"
$RuntimeDir = Join-Path $BaseDir "runtime\demo"
$PidFile = Join-Path $RuntimeDir "backend.pid"
$MainScript = Join-Path $BackendDir "main.py"
$ProcessMarker = "--buddy-claw-offline-demo"

function Get-DemoProcess([int]$ProcessId) {
    Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Test-OwnedProcess([int]$ProcessId) {
    $Process = Get-DemoProcess $ProcessId
    if ($null -eq $Process) { return $false }
    [string]$CommandLine = $Process.CommandLine
    $HasScript = $CommandLine.IndexOf($MainScript, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    $HasMarker = $CommandLine.IndexOf($ProcessMarker, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    return $HasScript -and $HasMarker
}

if (!(Test-Path -LiteralPath $PidFile)) {
    Write-Output "Demo service is not running."
    exit 0
}

$PidText = (Get-Content -LiteralPath $PidFile -Raw).Trim()
$DemoPid = 0
if (![int]::TryParse($PidText, [ref]$DemoPid)) { throw "Invalid PID file. Preserved for review: $PidFile" }

if ($null -eq (Get-DemoProcess $DemoPid)) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Output "Removed stale demo PID record."
    exit 0
}

if (!(Test-OwnedProcess $DemoPid)) { throw "PID $DemoPid does not belong to this demo service. No stop action was taken." }
Stop-Process -Id $DemoPid
Start-Sleep -Milliseconds 500

if ($null -eq (Get-DemoProcess $DemoPid)) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Output "Demo service stopped."
    exit 0
}

throw "Demo service is still running under PID $DemoPid. Check runtime\demo logs and handle it manually."
