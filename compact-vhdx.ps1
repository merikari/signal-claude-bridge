# Run this script as Administrator to compact docker_data.vhdx.
# Prerequisites: run fstrim inside Docker first (the bridge Makefile does this),
# then stop Docker Desktop and run this script.

$vhdx = "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx"
$compose = "$PSScriptRoot\docker-compose.yml"

Write-Host "=== Signal Claude Bridge — VHDX compaction ==="
Write-Host ""

# Step 1: fstrim inside the Docker VM to mark freed blocks
Write-Host "Step 1: Starting Docker Desktop for fstrim..."
Write-Host "  Waiting for Docker to be ready (up to 60 s)..."
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep 3
    $ready = (docker info 2>$null) -ne $null
} until ($ready -or (Get-Date) -gt $deadline)

if (-not $ready) {
    Write-Host "  ERROR: Docker didn't start in time. Open Docker Desktop manually and re-run."
    exit 1
}
Write-Host "  Docker ready."

Write-Host "Step 2: Running fstrim on Docker data disk..."
wsl -d docker-desktop -u root -e sh -c "fstrim -v /mnt/docker-desktop-disk"

Write-Host "Step 3: Stopping container and Docker Desktop..."
docker compose -f $compose stop 2>$null
Stop-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 10
wsl --shutdown
Start-Sleep -Seconds 5

$before = [math]::Round((Get-Item $vhdx).Length / 1GB, 1)
Write-Host "Step 4: Compacting VHDX (before: $before GB)..."

$tmp = "$env:TEMP\compact-vhdx.txt"
@"
select vdisk file="$vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@ | Set-Content -Encoding ASCII $tmp

diskpart /s $tmp
Remove-Item $tmp

$after = [math]::Round((Get-Item $vhdx).Length / 1GB, 1)
$saved = [math]::Round($before - $after, 1)
Write-Host ""
Write-Host "Done. Before: $before GB  After: $after GB  Saved: $saved GB"
Write-Host ""
Write-Host "Start Docker Desktop from the system tray, then run:"
Write-Host "  docker compose -f '$compose' start"
