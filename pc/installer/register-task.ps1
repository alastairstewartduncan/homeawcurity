# Called by the installer: makes A.S.D Home Security start with Windows (as a background task, before
# anyone signs in), adds the tray icon at sign-in, and opens its port on the home network.
# Usage: register-task.ps1 -AppDir <folder> -Port <port>
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

# The tray icon (camtray.exe) for whoever signs in: shows the logo by the clock, with a menu to
# open the viewer. It runs as the signed-in user ("Users" group), not as SYSTEM, so it can show
# in their taskbar. Registered as a task so the installer can also start it again after an update.
$users = ([System.Security.Principal.SecurityIdentifier]"S-1-5-32-545").Translate([System.Security.Principal.NTAccount]).Value
$trayAction = New-ScheduledTaskAction -Execute (Join-Path $AppDir "camtray.exe") -WorkingDirectory $AppDir
$trayTrigger = New-ScheduledTaskTrigger -AtLogOn
$trayPrincipal = New-ScheduledTaskPrincipal -GroupId $users -RunLevel Limited
$traySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances Parallel -Priority 5
Register-ScheduledTask -TaskName "Home Camera Tray" -Action $trayAction -Trigger $trayTrigger -Principal $trayPrincipal `
    -Settings $traySettings -Description "Shows the A.S.D Home Security icon in the system tray" -Force | Out-Null

Get-NetFirewallRule -DisplayName "Home Camera" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Home Camera" -Direction Inbound -Program $exe -Protocol TCP `
    -LocalPort $Port -Action Allow -Profile Private,Domain | Out-Null
