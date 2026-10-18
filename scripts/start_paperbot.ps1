# Start paperbot: check everything, test one pass (during US market hours), then
# register the unattended schedule (paperbot-schedule).
#
#   .\scripts\start_paperbot.ps1            run all steps, ask before registering
#   .\scripts\start_paperbot.ps1 -Yes       no confirmation before registering
#   .\scripts\start_paperbot.ps1 -SkipTest  skip the market-hours prefilter/cycle test
#
# IB Gateway can't be started from here: start it and log in to the paper
# account (DU...) first. Stops at the first failed step and registers nothing.
param(
    [switch]$Yes,
    [switch]$SkipTest
)

$Project = Split-Path -Parent $PSScriptRoot
$Scripts = Join-Path (Split-Path -Parent $Project) ".venv\Scripts"
$Python = Join-Path $Scripts "python.exe"

function Step($n, $text) { Write-Host ""; Write-Host "[$n] $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "STOPPED: $text" -ForegroundColor Red; exit 1 }
function Ok($text) { Write-Host "  ok: $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  warning: $text" -ForegroundColor Yellow }

function Invoke-Paperbot($name, [string[]]$argv = @()) {
    & (Join-Path $Scripts "$name.exe") @argv | Out-Host
    return $LASTEXITCODE
}

Set-Location $Project

# 1. venv and editable install
Step 1 "Python environment"
if (-not (Test-Path $Python)) { Fail "$Python not found. Create the venv first (README section 1)." }
foreach ($cmd in "paperbot-check", "paperbot-prefilter", "paperbot-cycle", "paperbot-schedule") {
    if (-not (Test-Path (Join-Path $Scripts "$cmd.exe"))) {
        Fail "$cmd not installed. Run: $Python -m pip install --no-deps -e $Project"
    }
}
Ok "venv at $Scripts"

# 2. Telegram test message
Step 2 "Telegram test message"
if (-not (Test-Path (Join-Path $Project ".env"))) {
    Warn ".env missing: no Telegram messages will be sent (copy .env.example to .env)"
} else {
    & $Python -c "from paperbot.reporting.notify import notify; notify('Test', 'paperbot start script: Telegram works')"
    if ($LASTEXITCODE -ne 0) { Warn "test message failed; see logs\notify_errors.log" }
    else { Ok "test message sent (check your phone; problems go to logs\notify_errors.log)" }
}

# 3. IB Gateway reachable on the configured port
Step 3 "IB Gateway"
$cfg = Get-Content (Join-Path $Project "config\config.json") -Raw | ConvertFrom-Json
$client = New-Object System.Net.Sockets.TcpClient
try {
    $connect = $client.BeginConnect($cfg.host, [int]$cfg.port, $null, $null)
    $reachable = $connect.AsyncWaitHandle.WaitOne(3000) -and $client.Connected
} catch { $reachable = $false } finally { $client.Close() }
if (-not $reachable) {
    Fail ("nothing is listening on $($cfg.host):$($cfg.port). Start IB Gateway, log in to the " +
          "PAPER account, and check API settings (socket port $($cfg.port), Read-Only API off).")
}
Ok "Gateway is listening on $($cfg.host):$($cfg.port)"
Write-Host "  Make sure Gateway's daily auto-restart is on, so it stays logged in overnight."

# 4. STOP file
Step 4 "STOP file"
$stopFile = Join-Path $Project "STOP"
if (Test-Path $stopFile) { Remove-Item $stopFile; Ok "STOP file deleted" }
else { Ok "no STOP file" }

# 5. Offline setup check
Step 5 "Setup check (paperbot-check)"
if ((Invoke-Paperbot "paperbot-check") -ne 0) { Fail "paperbot-check found problems (see above)." }

# 6-7. Prefilter and one dry-run pass, only while the US market is open
$et = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "Eastern Standard Time")
$minutes = $et.Hour * 60 + $et.Minute
$weekday = $et.DayOfWeek -ne "Saturday" -and $et.DayOfWeek -ne "Sunday"
$marketOpen = $weekday -and $minutes -ge (9 * 60 + 30) -and $minutes -lt (16 * 60)
Step "6-7" ("Market-hours test (now {0:ddd HH:mm} ET)" -f $et)
if ($SkipTest) {
    Warn "skipped (-SkipTest)"
} elseif (-not $marketOpen) {
    Warn ("US market is closed, so the prefilter and the dry-run pass were skipped. " +
          "Re-run this script between 09:30 and 16:00 ET (Mon-Fri) to test them first.")
} else {
    Write-Host "  prefilter preview..."
    if ((Invoke-Paperbot "paperbot-prefilter" @("--dry-run")) -ne 0) { Fail "prefilter --dry-run failed." }
    Write-Host "  prefilter (writes data\watchlist.txt)..."
    if ((Invoke-Paperbot "paperbot-prefilter") -ne 0) { Fail "prefilter failed." }
    Write-Host "  one dry-run pass (connects, evaluates, sends NO orders)..."
    if ((Invoke-Paperbot "paperbot-cycle") -ne 0) { Fail "dry-run pass failed; see logs\cycle_errors.log." }
    Ok "dry run done. What it would have done is in logs\bot.log and logs\safety_log.jsonl"
    if ($minutes -lt (10 * 60 + 5) -or $minutes -ge (15 * 60 + 30)) {
        Warn "outside the 10:05-15:30 ET entry window, so no entries were evaluated in this pass."
    }
}

# 8. Register the schedule
Step 8 "Register the unattended schedule (paperbot-schedule)"
Write-Host "  From now on HT_Cycle runs with --submit: the bot PLACES ORDERS on the paper" -ForegroundColor Yellow
Write-Host "  account every US session, while this PC is on and you are logged in." -ForegroundColor Yellow
if (-not $Yes) {
    $answer = Read-Host "  Type YES to register"
    if ($answer -ne "YES") { Write-Host "Not registered. Nothing scheduled."; exit 0 }
}
if ((Invoke-Paperbot "paperbot-schedule") -ne 0) { Fail "some tasks failed to register (see above)." }

Write-Host ""
Write-Host "paperbot is scheduled. Keep the PC plugged in, logged in, and IB Gateway running." -ForegroundColor Green
Write-Host "Status: dashboard\index.html and Telegram. To stop: scripts\stop_paperbot.ps1"
