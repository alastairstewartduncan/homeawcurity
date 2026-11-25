@echo off
rem Searches the camera for its video addresses and saves them, then restarts Home Camera.
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" & exit /b)
title Home Camera - find camera
echo This looks for your camera's video and saves it in Home Camera's settings.
echo.
set /p CAMIP=Camera IP address (for example 192.168.1.50): 
set CAMUSER=admin
set /p CAMUSER=Camera user name (press Enter for admin): 
"%~dp0camhub.exe" --config "%ProgramData%\HomeCamera\config.json" find-stream %CAMIP% --user "%CAMUSER%" --save
if errorlevel 1 goto end
schtasks /End /TN "Home Camera" >nul 2>&1
taskkill /F /T /IM camhub.exe >nul 2>&1
timeout /t 2 /nobreak >nul
schtasks /Run /TN "Home Camera" >nul
echo Home Camera restarted with the new settings.
:end
echo.
pause
