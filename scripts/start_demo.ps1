$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir = Split-Path -Parent $ScriptDir
$BackendDir = Join-Path $BaseDir "backend"
$PythonBin = Join-Path $BaseDir ".venv\Scripts\python.exe"
$DistIndex = Join-Path $BaseDir "frontend\dist\index.html"
$RuntimeDir = Join-Path $BaseDir "runtime\demo"
$PidFile = Join-Path $RuntimeDir "backend.pid"
$StdoutLog = Join-Path $RuntimeDir "backend.stdout.log"
$StderrLog = Join-Path $RuntimeDir "backend.stderr.log"
$DemoDataDir = Join-Path $RuntimeDir "data"
$MainScript = Join-Path $BackendDir "main.py"
$Port = if ($env:BUDDY_CLAW_PORT) { [int]$env:BUDDY_CLAW_PORT } else { 8000 }
$Url = "http://127.0.0.1:$Port"
$ProcessMarker = "--buddy-claw-offline-demo"

function Fail([string]$Message) { throw $Message }

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

if (!(Test-Path -LiteralPath $PythonBin)) { Fail "Missing .venv\Scripts\python.exe. Run setup_mac.sh first." }
if (!(Test-Path -LiteralPath $DistIndex)) { Fail "Missing frontend build output. Run setup_mac.sh first." }
New-Item -ItemType Directory -Force -Path $RuntimeDir, $DemoDataDir | Out-Null

$env:SCORING_DATA_DIR = $DemoDataDir
& $PythonBin (Join-Path $BaseDir "scripts\init_demo.py")
if ($LASTEXITCODE -ne 0) { Fail "Synthetic demo initialization failed." }

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPidText = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $ExistingPid = 0
    if (![int]::TryParse($ExistingPidText, [ref]$ExistingPid)) { Fail "PID 文件内容无效：$PidFile" }
    if ($null -ne (Get-DemoProcess $ExistingPid)) {
        if (!(Test-OwnedProcess $ExistingPid)) { Fail "PID $ExistingPid belongs to another process. No stop action was taken." }
        Write-Output "Demo service is already running at $Url (PID $ExistingPid)."
        exit 0
    }
    Remove-Item -LiteralPath $PidFile -Force
}

$PortOwner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($null -ne $PortOwner) { Fail "Port $Port is already in use. This script does not kill port processes." }

$env:DATA_DIR = $DemoDataDir
$env:SCORING_PORT = "$Port"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:LLM_API_KEY = ""
$env:DASHSCOPE_API_KEY = ""
$env:LLM_API_URL = ""
$env:LLM_MODEL = ""
$env:FEATURE_NEW_PARSER = "true"
$env:AIGC_OCR = "false"
$env:FEWSHOT = "false"
$env:ANCHOR_V2 = "true"
$env:MONITOR_GUARD = "true"
$env:DETERMINISTIC_MODE = "true"
$env:ENABLE_BATCH_SCORING = "false"

$Process = Start-Process -FilePath $PythonBin `
    -ArgumentList @($MainScript, $ProcessMarker) `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -PassThru
$Process.Id | Set-Content -LiteralPath $PidFile -NoNewline

$Ready = $false
for ($i = 0; $i -lt 30; $i++) {
    if ($null -eq (Get-DemoProcess $Process.Id)) { break }
    try {
        $Health = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 2
        if ($Health.StatusCode -eq 200 -and $Health.Content -match "healthy") {
            $Ready = $true
            break
        }
    } catch { }
    Start-Sleep -Seconds 1
}

if (!$Ready) {
    Write-Error "The service did not become ready within 30 seconds. Check $StdoutLog and $StderrLog."
    if (Test-OwnedProcess $Process.Id) { Stop-Process -Id $Process.Id }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    exit 1
}

$PortOwner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $PortOwner -or !(Test-OwnedProcess $PortOwner.OwningProcess)) {
    Write-Error "The process listening on port $Port could not be verified as this demo service."
    exit 1
}
$PortOwner.OwningProcess | Set-Content -LiteralPath $PidFile -NoNewline

Write-Output "Demo service started at $Url."
Write-Output "Data directory: $DemoDataDir"
Write-Output "Stop with: powershell -ExecutionPolicy Bypass -File scripts\stop_demo.ps1"
if ($env:BUDDY_CLAW_NO_BROWSER -ne "1") {
    Start-Process $Url
}
