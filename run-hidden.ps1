# Called by start-bridge.vbs — no console, app writes its own rotated log.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Load .env
if (Test-Path .env) {
    Get-Content .env | Where-Object { $_ -match '^[A-Z_]' -and $_ -match '=' } | ForEach-Object {
        $name, $value = $_ -split '=', 2
        [System.Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
    }
}

# Single-instance guard: kill any pre-existing bridge processes started from this directory.
# Matches `pythonw.exe app.py` whose CommandLine references this script's parent dir.
$selfPid = $PID
$here = $PSScriptRoot
try {
    $stale = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'app\.py' -and $_.CommandLine -like "*$here*" } |
        Where-Object { $_.ProcessId -ne $selfPid }
    foreach ($p in $stale) {
        Write-Host "Stopping stale bridge pythonw.exe PID=$($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    # Also try venv pythonw via path match (covers cases where Get-CimInstance returns generic .exe path)
    $venvStale = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction Stop |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$here\.venv\*" } |
        Where-Object { $_.ProcessId -ne $selfPid }
    foreach ($p in $venvStale) {
        Write-Host "Stopping stale venv pythonw.exe PID=$($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} catch {
    Write-Host "Single-instance check skipped: $_"
}

# Ensure venv exists
if (-not (Test-Path .venv)) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
}

# app.py configures its own RotatingFileHandler at logs\bridge.log.
# Redirect any uncaught stderr (e.g. tracebacks during startup) to a separate file.
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "logs") | Out-Null
$stderrLog = Join-Path $PSScriptRoot "logs\bridge-stderr.log"
$stdoutLog = Join-Path $PSScriptRoot "logs\bridge-out.log"

$pythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
Start-Process -FilePath $pythonw `
    -ArgumentList "app.py" `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -NoNewWindow `
    -Wait
