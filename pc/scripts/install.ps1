# A.S.D Home Security installer for Windows 10/11.
# Right-click this file and choose "Run with PowerShell". It asks for admin rights.
#
# What it does:
#   1. Installs Python 3.12 and FFmpeg with winget if they are missing
#   2. Sets up the recorder in its own Python environment (pc\.venv)
#   3. Downloads the person detection model
#   4. Creates config.json, sets your viewer password, and helps find the camera's stream
#   5. Starts the recorder automatically when Windows starts (even before anyone logs in)
#   6. Opens port 8080 on the home network and adds an "A.S.D Home Security" desktop shortcut

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

$Pc = Split-Path -Parent $PSScriptRoot
Set-Location $Pc
$TaskName = "Home Camera"
function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

# ---- 1. Python and FFmpeg ----------------------------------------------------
Step "Checking Python"
$py = $null
foreach ($cand in @("py -3.12", "py -3.11", "python")) {
    try {
        $v = & cmd /c "$cand --version 2>&1"
        if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(1[0-9])") { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Write-Host "Installing Python 3.12..."
    winget install -e --id Python.Python.3.12 --scope machine --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $py = "py -3.12"
}
Write-Host "Using $py"

Step "Checking FFmpeg"
$bin = Join-Path $Pc "bin"
New-Item -ItemType Directory -Force $bin | Out-Null
if (-not (Test-Path (Join-Path $bin "ffmpeg.exe"))) {
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ff) {
        Write-Host "Installing FFmpeg..."
        winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
        if (-not $ff) {
            $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages", "$env:ProgramFiles\WinGet\Packages" `
                -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($found) { $ff = @{ Source = $found.FullName } }
        }
    }
    if (-not $ff) { throw "FFmpeg could not be installed. Install it from https://www.gyan.dev/ffmpeg/builds/ and run this again." }
    # Keep a private copy so the recorder finds it even when no one is logged in.
    $ffDir = Split-Path -Parent $ff.Source
    Copy-Item (Join-Path $ffDir "ffmpeg.exe"), (Join-Path $ffDir "ffprobe.exe") $bin -Force
}

# ---- 2. Python environment ---------------------------------------------------
Step "Setting up the recorder"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & cmd /c "$py -m venv .venv"; if ($LASTEXITCODE) { throw "Could not create .venv" } }
$vpy = Join-Path $Pc ".venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE) { throw "Installing Python packages failed" }

# ---- 3. Person detection model ------------------------------------------------
Step "Downloading the person detection model"
$models = Join-Path $Pc "models"
New-Item -ItemType Directory -Force $models | Out-Null
$base = "https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master"
foreach ($f in @("deploy.prototxt", "mobilenet_iter_73000.caffemodel")) {
    $dest = Join-Path $models $f
    if (-not (Test-Path $dest)) { Invoke-WebRequest "$base/$f" -OutFile $dest -UseBasicParsing }
}
# Face recognition (Smart tab): finding faces, and telling family from strangers.
$zoo = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
foreach ($f in @("face_detection_yunet/face_detection_yunet_2023mar.onnx",
                 "face_recognition_sface/face_recognition_sface_2021dec.onnx")) {
    $dest = Join-Path $models (Split-Path $f -Leaf)
    if (-not (Test-Path $dest)) { Invoke-WebRequest "$zoo/$f" -OutFile $dest -UseBasicParsing }
}

# ---- 4. Settings ---------------------------------------------------------------
Step "Settings"
if (-not (Test-Path "config.json")) { Copy-Item "config.example.json" "config.json" }
& $vpy -c "import json;p='config.json';c=json.load(open(p));c['ffmpeg_path']=r'$bin\ffmpeg.exe';json.dump(c,open(p,'w'),indent=2)"

$cfg = Get-Content config.json -Raw | ConvertFrom-Json
if (-not $cfg.server.password_hash) {
    Write-Host "Choose the password you'll use to sign in on the PC and the phone."
    & $vpy -m camhub set-password
}

$ip = Read-Host "Camera IP address (find it in the Hiseeu app or your router), or press Enter to skip"
if ($ip) {
    $user = Read-Host "Camera user name (press Enter for admin)"
    if (-not $user) { $user = "admin" }
    & $vpy -m camhub find-stream $ip --user $user
}
Write-Host "`nNow put the camera's main_url, sub_url and ONVIF details into config.json. Save and close Notepad to carry on."
Start-Process notepad.exe (Join-Path $Pc "config.json") -Wait

# ---- 5. Start with Windows --------------------------------------------------------
Step "Starting with Windows"
$action = New-ScheduledTaskAction -Execute $vpy -Argument "-m camhub run" -WorkingDirectory $Pc
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force | Out-Null

# ---- 6. Network and shortcut ---------------------------------------------------------
Step "Opening port 8080 on the home network"
$cfg = Get-Content config.json -Raw | ConvertFrom-Json
$port = $cfg.server.port
Get-NetFirewallRule -DisplayName "Home Camera" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Home Camera" -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private | Out-Null

$scheme = if ($cfg.server.https_cert) { "https" } else { "http" }
$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
$desktop = [Environment]::GetFolderPath("CommonDesktopDirectory")
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop "A.S.D Home Security.lnk"))
$lnk.TargetPath = $edge
$lnk.Arguments = "--app=${scheme}://localhost:$port"
$lnk.IconLocation = "$edge,0"
$lnk.Save()

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Start-Sleep 5
Write-Host "`nAll done. A.S.D Home Security is running and will start by itself with Windows." -ForegroundColor Green
Write-Host "Open it with the 'A.S.D Home Security' icon on the desktop. On your phone, use http://<this PC's IP>:$port"
Write-Host "Logs are in $Pc\logs\camhub.log"
Read-Host "Press Enter to close"
