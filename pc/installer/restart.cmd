@echo off
rem Restarts Home Camera, for example after changing config.json.
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" & exit /b)
echo Restarting Home Camera...
schtasks /End /TN "Home Camera" >nul 2>&1
taskkill /F /T /IM camhub.exe >nul 2>&1
timeout /t 2 /nobreak >nul
schtasks /Run /TN "Home Camera" >nul
echo Done.
timeout /t 3 >nul
