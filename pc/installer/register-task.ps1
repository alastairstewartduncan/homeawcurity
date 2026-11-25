# Called by the installer: makes Home Camera start with Windows (as a background task) and
# opens its port on the home network. Usage: register-task.ps1 -AppDir <folder> -Port <port>
param([Parameter(Mandatory)] [string]$AppDir, [int]$Port = 8080)
$ErrorActionPreference = "Stop"
$exe = Join-Path $AppDir "camhub.exe"
$config = Join-Path $env:ProgramData "HomeCamera\config.json"
$action = New-ScheduledTaskAction -Execute $exe -Argument "--config `"$config`" run" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Home Camera" -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Records the home camera when motion is detected" -Force | Out-Null

Get-NetFirewallRule -DisplayName "Home Camera" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Home Camera" -Direction Inbound -Program $exe -Protocol TCP `
    -LocalPort $Port -Action Allow -Profile Private,Domain | Out-Null
