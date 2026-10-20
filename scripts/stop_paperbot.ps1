# Stop paperbot on Windows: the same as `paper-bot stop` (also works on macOS/Linux).
# Protective stops always stay at IBKR, so open positions remain protected.
#
#   .\scripts\stop_paperbot.ps1                graceful stop (waits up to -Timeout seconds)
#   .\scripts\stop_paperbot.ps1 -CancelOrders  also cancel paperbot's working entry/exit orders
#
# A service installed with `paper-bot service install` is stopped the same way and
# does not restart until `paper-bot start` (which clears the STOP file) is run.
param(
    [switch]$CancelOrders,
    [int]$Timeout = 120
)

$Project = Split-Path -Parent $PSScriptRoot
Set-Location $Project
$Python = @((Join-Path $Project ".venv\Scripts\python.exe"),
            (Join-Path (Split-Path -Parent $Project) ".venv\Scripts\python.exe")) |
          Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Python) { $Python = "python" }

& $Python -m paperbot.cli stop --timeout $Timeout
$code = $LASTEXITCODE
if ($CancelOrders) { & $Python -m paperbot.cli kill }
exit $code
