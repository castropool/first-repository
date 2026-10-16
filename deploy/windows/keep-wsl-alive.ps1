# WSL2 shuts a distro down shortly after the last Windows-side process using it exits, which
# also stops systemd user services. This registers a logon task that keeps the distro running.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\keep-wsl-alive.ps1 -Distro Ubuntu
param([string]$Distro = "Ubuntu", [string]$TaskName = "WSL-KeepAlive")
$ErrorActionPreference = "Stop"
$action   = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro --exec /bin/sleep infinity"
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -Hidden
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Keep WSL distro $Distro running so systemd services stay up" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Registered '$TaskName' - WSL distro '$Distro' will stay running while you are logged on."
