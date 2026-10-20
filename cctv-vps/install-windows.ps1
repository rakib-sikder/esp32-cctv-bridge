# Registers the recorder and viewer to start with Windows.
#
# Run from an elevated PowerShell:
#     powershell -ExecutionPolicy Bypass -File install-windows.ps1
#
# Undo with:
#     Unregister-ScheduledTask -TaskName CCTV-Recorder -Confirm:$false
#     Unregister-ScheduledTask -TaskName CCTV-Viewer   -Confirm:$false

param(
  [string]$CamIp    = "192.168.68.50",
  [string]$ClipDir  = "$env:USERPROFILE\cctv-clips",
  [int]   $ViewPort = 8080,
  [int]   $Retention= 14
)

$ErrorActionPreference = "Stop"

$py = "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe"   # pythonw = no console window
if (-not (Test-Path $py)) {
  $py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
}
if (-not $py) { throw "pythonw.exe not found - install Python 3 first" }

$ffmpeg = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ffmpeg.exe"
if (-not (Test-Path $ffmpeg)) {
  $ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
}
if (-not $ffmpeg) { throw "ffmpeg not found - winget install Gyan.FFmpeg" }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$recorder = Join-Path $here "recorder.py"
$viewer   = Join-Path $here "viewer.py"
foreach ($f in @($recorder, $viewer)) {
  if (-not (Test-Path $f)) { throw "missing $f" }
}

New-Item -ItemType Directory -Force -Path $ClipDir | Out-Null

Write-Host "python : $py"
Write-Host "ffmpeg : $ffmpeg"
Write-Host "clips  : $ClipDir"
Write-Host ""

# Scheduled tasks cannot carry environment variables, so each one is launched
# through cmd with the settings set inline.
function Register-CctvTask {
  param([string]$Name, [string]$Script, [string]$ExtraEnv)

  $envSet = "set CAM_IP=$CamIp&& set CLIP_DIR=$ClipDir&& set FFMPEG=$ffmpeg&& set RETENTION_DAYS=$Retention&& $ExtraEnv"
  $cmd = "/c `"$envSet `"$py`" `"$Script`"`""

  $action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd
  $trigger  = New-ScheduledTaskTrigger -AtLogOn
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

  Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
      -Settings $settings -Description "V380 CCTV $Name" | Out-Null
  Write-Host "registered: $Name"
}

Register-CctvTask -Name "CCTV-Recorder" -Script $recorder -ExtraEnv "set MOTION_SOURCE=video&&"
Register-CctvTask -Name "CCTV-Viewer"   -Script $viewer   -ExtraEnv "set VIEWER_PORT=$ViewPort&&"

Write-Host ""
Write-Host "Starting both now..."
Start-ScheduledTask -TaskName "CCTV-Recorder"
Start-ScheduledTask -TaskName "CCTV-Viewer"
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName "CCTV-*" | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Viewer: http://127.0.0.1:$ViewPort"
