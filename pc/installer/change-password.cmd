@echo off
rem Changes the password for the viewer and phone app. Signs out every device.
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" & exit /b)
title Home Camera - change password
"%~dp0camhub.exe" --config "%ProgramData%\HomeCamera\config.json" set-password
schtasks /End /TN "Home Camera" >nul 2>&1
taskkill /F /T /IM camhub.exe >nul 2>&1
timeout /t 2 /nobreak >nul
schtasks /Run /TN "Home Camera" >nul
echo.
pause
