# Start paperbot on Windows: the same as `paper-bot start` (also works on macOS/Linux).
#
#   .\scripts\start_paperbot.ps1                      strategy menu, confirmation, trading day
#   .\scripts\start_paperbot.ps1 -Yes                 skip the confirmation
#   .\scripts\start_paperbot.ps1 -Strategy KEY        skip the menu
#   .\scripts\start_paperbot.ps1 -DryRun              log orders instead of sending them
#   .\scripts\start_paperbot.ps1 -Python PATH         use this python.exe
#
# Interpreter: -Python, else $env:PAPERBOT_PYTHON, else the one .venv found in the
# project folder or its parent. If BOTH .venv folders exist the script stops and
# asks you to choose (so the bot never runs on the wrong environment silently).
# The chosen interpreter is printed and must import paperbot from this project.
#
# Start IB Gateway and log in first. To start automatically at log-on instead,
# run once: paper-bot service install
param(
    [switch]$Yes,
    [switch]$DryRun,
    [string]$Strategy = "",
    [string]$Python = ""
)

$Project = Split-Path -Parent $PSScriptRoot
Set-Location $Project
if (-not $Python) { $Python = $env:PAPERBOT_PYTHON }
if (-not $Python) {
    $found = @((Join-Path $Project ".venv\Scripts\python.exe"),
               (Join-Path (Split-Path -Parent $Project) ".venv\Scripts\python.exe")) |
             Where-Object { Test-Path $_ }
    if (@($found).Count -gt 1) {
        Write-Host "More than one Python environment found:"
        $found | ForEach-Object { Write-Host "  $_" }
        Write-Host "Choose one: -Python <path>, or set PAPERBOT_PYTHON. Nothing started."
        exit 2
    }
    $Python = @($found) | Select-Object -First 1
}
if (-not $Python) { $Python = "python" }

# The interpreter must run THIS project's paperbot (an editable install of src\).
$where = $null
try { $where = & $Python -c "import pathlib, paperbot; print(pathlib.Path(paperbot.__file__).resolve())" 2>$null }
catch { $where = $null }
$expected = (Join-Path $Project "src\paperbot")
if ($LASTEXITCODE -ne 0 -or -not $where -or -not ([string]$where).StartsWith($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "Python: $Python"
    Write-Host "It does not run this project's paperbot (found: $where; expected under $expected)."
    Write-Host "Install it with: $Python -m pip install -e `"$Project`"   Nothing started."
    exit 2
}
Write-Host "Python: $Python (paperbot from $expected)"

$argv = @("-m", "paperbot.cli", "start")
if ($Yes) { $argv += "--yes" }
if ($DryRun) { $argv += "--dry-run" }
if ($Strategy) { $argv += @("--strategy", $Strategy) }
& $Python @argv
exit $LASTEXITCODE
