# Registers TwNewsMonitor as a Windows Task Scheduler task that starts at logon and restarts on failure.
# Run in the repo root (no elevation needed for the current user):
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1
param([string]$TaskName = "TwNewsMonitor")
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"   # no console window
if (-not (Test-Path $Pythonw)) { throw "virtualenv not found at $Pythonw - create it first (see README)" }

$action   = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Root\main.py`" --config `"$Root\config.yaml`"" -WorkingDirectory $Root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "TwNewsMonitor - Taiwan news briefings" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Registered and started task '$TaskName'. Logs: $Root\logs\twnews.log"
