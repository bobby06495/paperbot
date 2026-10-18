# Stop paperbot. Protective stops always stay at IBKR, so open positions remain protected.
#
#   .\scripts\stop_paperbot.ps1                stop for good: STOP file, wait for a running
#                                             pass to exit, remove the HT_* tasks, restore sleep
#   .\scripts\stop_paperbot.ps1 -Pause         only pause: STOP file + wait; tasks stay registered
#                                             (delete the STOP file to resume)
#   .\scripts\stop_paperbot.ps1 -CancelOrders  also cancel paperbot's working entry/exit orders
#                                             (paperbot-kill; needs IB Gateway if no pass is running)
#
# Never kills a process and never cancels protective stops. For a global cancel of
# every order (stops included) run paperbot-kill --all yourself.
param(
    [switch]$Pause,
    [switch]$CancelOrders,
    [int]$Timeout = 300
)

$Project = Split-Path -Parent $PSScriptRoot
$Scripts = Join-Path (Split-Path -Parent $Project) ".venv\Scripts"
if (-not (Test-Path (Join-Path $Scripts "paperbot-stop.exe"))) {
    Write-Host "STOPPED: paperbot commands not found in $Scripts" -ForegroundColor Red
    exit 1
}
Set-Location $Project
$exitCode = 0

# STOP file first: every scheduled run skips itself from now on, and a running
# bot or pass cancels its entry/exit orders and exits on its next loop.
New-Item -ItemType File -Path (Join-Path $Project "STOP") -Force | Out-Null
Write-Host "STOP file created: no new runs will start." -ForegroundColor Cyan

if ($CancelOrders) {
    & (Join-Path $Scripts "paperbot-kill.exe")
    if ($LASTEXITCODE -ne 0) { Write-Host "paperbot-kill failed (is IB Gateway running?)" -ForegroundColor Yellow; $exitCode = 1 }
}

# Wait for a bot or scheduled pass that is already running (a force-close pass
# near 15:51 ET can take several minutes).
& (Join-Path $Scripts "paperbot-stop.exe") --timeout $Timeout
if ($LASTEXITCODE -ne 0) { $exitCode = 1 }

if ($Pause) {
    Write-Host ""
    Write-Host "Paused. Tasks are still registered; delete the STOP file to resume." -ForegroundColor Green
    exit $exitCode
}

& (Join-Path $Scripts "paperbot-unschedule.exe")
if ($LASTEXITCODE -ne 0) { $exitCode = 1 }

Write-Host ""
Write-Host "paperbot stopped and unscheduled. Open positions keep their GTC stops at IBKR." -ForegroundColor Green
Write-Host "The STOP file is left in place; scripts\start_paperbot.ps1 deletes it when you restart."
exit $exitCode
