# Restarts A.S.D Home Security, for example after editing config.json. Run with PowerShell.
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}
Stop-ScheduledTask -TaskName "Home Camera" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*camhub run*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-Process ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force
Start-ScheduledTask -TaskName "Home Camera"
Write-Host "A.S.D Home Security restarted."
Start-Sleep 2
