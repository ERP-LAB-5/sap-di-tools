<#
.SYNOPSIS
  Start di-replication-sync on Windows.

.DESCRIPTION
  Creates .venv on first run (and reinstalls when requirements.txt changed),
  stops any instance already holding the port, then starts a fresh one and
  opens the browser. Anything not listed below goes to the app as it is.

  Core-owned (D-LAB-5 tool template): `copier update` rewrites this file.

.EXAMPLE
  .\run.ps1
  .\run.ps1 -Port 9000
  .\run.ps1 -Stop            # just shut the running one down
  .\run.ps1 --no-update-check
#>
# PositionalBinding off: without it PowerShell hands the first unnamed argument
# (say --no-update-check) to -Port by position and fails converting it to an int,
# instead of letting it fall through to $Rest for the app.
[CmdletBinding(PositionalBinding = $false)]
param(
    [int]$Port = 8766,
    [string]$Bind = "127.0.0.1",
    [switch]$Stop,           # stop whatever is running and exit
    [switch]$NoBrowser,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
# a Python environment inherited from elsewhere must not shadow the venv
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

function Get-ServerPids {
    <# Whatever owns the port, plus any stray copy of this app from an earlier run. #>
    $found = @()
    try {
        $found += (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop).OwningProcess
    } catch {
        # Get-NetTCPConnection is missing on older hosts; fall back to netstat
        $found += (netstat -ano | Select-String ":$Port\s+.*LISTENING" |
                   ForEach-Object { ($_ -split '\s+')[-1] })
    }
    # match our own app only, never someone else's project on this machine
    $mine = "di_replication_sync.app"
    $found += Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
              Where-Object { $_.CommandLine -match [regex]::Escape($mine) -and
                             $_.CommandLine -match [regex]::Escape($PSScriptRoot) } |
              ForEach-Object { $_.ProcessId }
    $found | Where-Object { $_ } | Sort-Object -Unique
}

function Stop-Server {
    $pids = Get-ServerPids
    if (-not $pids) { Write-Host "  nothing running on port $Port"; return }
    # ask the server to close itself first, so it can finish a write in flight
    try {
        Invoke-RestMethod -Method Post -TimeoutSec 2 `
            -Uri "http://127.0.0.1:$Port/api/shutdown" | Out-Null
    } catch { }
    Start-Sleep -Milliseconds 400
    foreach ($processId in $pids) {
        if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
            Write-Host "  killing pid $processId"
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        } else {
            Write-Host "  stopped pid $processId"
        }
    }
    Start-Sleep -Milliseconds 300
}

# ---------------------------------------------------------------- stop only --
if ($Stop) { Stop-Server; exit 0 }

# ------------------------------------------------------------------- python --
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    # Windows PowerShell 5.1 has no ?: ternary, so keep this an if/else
    if (Get-Command py -ErrorAction SilentlyContinue)          { $bootstrap = "py" }
    elseif (Get-Command python -ErrorAction SilentlyContinue)  { $bootstrap = "python" }
    else { throw "no Python found on PATH — install Python 3.10+ from python.org" }
    Write-Host "  creating .venv ..."
    & $bootstrap -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "could not create .venv — is Python 3.10+ installed?" }
    & $py -m pip install --quiet --upgrade pip
}
$stamp = Join-Path $PSScriptRoot ".venv\.requirements.txt"
$wanted = Get-Content -Raw (Join-Path $PSScriptRoot "requirements.txt")
$had = if (Test-Path $stamp) { Get-Content -Raw $stamp } else { "" }
if ($wanted -ne $had) {
    Write-Host "  installing requirements ..."
    & $py -m pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "could not install requirements.txt — check your network or proxy" }
    Set-Content -NoNewline -Path $stamp -Value $wanted
}

# ------------------------------------------------------------------ restart --
Stop-Server
Write-Host "  starting di-replication-sync on http://${Bind}:$Port"
$appArgs = @("-m", "di_replication_sync.app", "--host", $Bind, "--port", $Port) + $Rest
$proc = Start-Process -FilePath $py -ArgumentList $appArgs `
    -WorkingDirectory $PSScriptRoot -PassThru

# wait for it to answer before opening a tab at a dead port
$ready = $false
foreach ($attempt in 1..40) {
    Start-Sleep -Milliseconds 250
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 -UseBasicParsing | Out-Null
        $ready = $true
        break
    } catch { }
}

if (-not $ready) {
    throw "di-replication-sync did not come up on port $Port (pid $($proc.Id))"
}

Write-Host "  ready — pid $($proc.Id)"
Write-Host "  stop it with the red Stop button, or:  .\run.ps1 -Stop"
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/" }
