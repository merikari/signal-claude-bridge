# Register the bridge as a Windows scheduled task that runs at logon.
# Uses a VBScript launcher so there is no visible terminal window.
# Run this once (elevation not strictly required but recommended).

$ErrorActionPreference = "Stop"
$taskName = "SignalClaudeBridge"
$vbs = Join-Path $PSScriptRoot "start-bridge.vbs"

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$vbs`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Signal→Claude Code bridge (hidden, logs to logs\bridge.log)" `
    -Force

Write-Host "Registered '$taskName'. Starting now..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep 3
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
