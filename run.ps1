# Start the Signal→Claude bridge polling daemon.
# Reads env vars from .env in the same directory.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Test-Path .env) {
    Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object {
        $name, $value = $_ -split '=', 2
        [System.Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
    }
}

if (-not (Test-Path .venv)) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

.\.venv\Scripts\python.exe app.py
