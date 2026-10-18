"""Run the start/stop PowerShell scripts from the venv's Scripts folder.

    paperbot-start [-Yes] [-SkipTest]                    scripts/start_paperbot.ps1
    paperbot-shutdown [-Pause] [-CancelOrders] [-Timeout N]  scripts/stop_paperbot.ps1

Arguments are passed through unchanged. The execution policy is bypassed for
that one PowerShell process only.
"""
import subprocess
import sys

from paperbot import paths


def _run_script(name: str) -> int:
    script = paths.PROJECT_ROOT / "scripts" / name
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(script), *sys.argv[1:]])


def start() -> int:
    return _run_script("start_paperbot.ps1")


def shutdown() -> int:
    return _run_script("stop_paperbot.ps1")


if __name__ == "__main__":
    sys.exit(start())
