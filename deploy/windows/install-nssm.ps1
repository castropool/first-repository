# Installs TwNewsMonitor as a Windows service with NSSM (https://nssm.cc).
# Run from an elevated PowerShell in the repo root:  powershell -ExecutionPolicy Bypass -File deploy\windows\install-nssm.ps1
param(
    [string]$Nssm = "nssm.exe",
    [string]$ServiceName = "TwNewsMonitor"
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "virtualenv not found at $Python - create it first (see README)" }

& $Nssm install $ServiceName $Python "`"$Root\main.py`" --config `"$Root\config.yaml`""
& $Nssm set $ServiceName AppDirectory $Root
& $Nssm set $ServiceName DisplayName "TwNewsMonitor (Taiwan news briefings)"
& $Nssm set $ServiceName Start SERVICE_AUTO_START
& $Nssm set $ServiceName AppEnvironmentExtra "PYTHONUNBUFFERED=1" "PYTHONUTF8=1"
# Graceful stop: Ctrl+C first and give in-flight jobs 30 s.
& $Nssm set $ServiceName AppStopMethodConsole 30000
& $Nssm set $ServiceName AppExit Default Restart
& $Nssm set $ServiceName AppRestartDelay 15000
New-Item -ItemType Directory -Force -Path "$Root\logs" | Out-Null
& $Nssm set $ServiceName AppStdout "$Root\logs\service-stdout.log"
& $Nssm set $ServiceName AppStderr "$Root\logs\service-stderr.log"
& $Nssm set $ServiceName AppRotateFiles 1
& $Nssm set $ServiceName AppRotateBytes 10485760
& $Nssm start $ServiceName
Write-Host "Installed and started $ServiceName. Manage with: nssm restart|stop|remove $ServiceName"
