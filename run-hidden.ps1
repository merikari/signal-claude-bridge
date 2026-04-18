# Called by start-bridge.vbs — no console, logs to file instead.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Load .env
if (Test-Path .env) {
    Get-Content .env | Where-Object { $_ -match '^[A-Z_]' -and $_ -match '=' } | ForEach-Object {
        $name, $value = $_ -split '=', 2
        [System.Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
    }
}

# Ensure venv exists
if (-not (Test-Path .venv)) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
}

# Run with pythonw (no console window); redirect output to rolling log
$log = Join-Path $PSScriptRoot "logs\bridge.log"
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "logs") | Out-Null

$pythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
Start-Process -FilePath $pythonw `
    -ArgumentList "app.py" `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput ($log -replace '\.log$', '-out.log') `
    -RedirectStandardError $log `
    -NoNewWindow `
    -Wait
