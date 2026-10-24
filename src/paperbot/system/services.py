"""Run paper-bot automatically at boot/login, per operating system.

    paper-bot service install [--dry-run]     write + register the service
    paper-bot service uninstall [--dry-run]
    paper-bot service status

  Linux    systemd user unit  ~/.config/systemd/user/paper-bot.service
           (restarts on failure; on a server also run once:
            sudo loginctl enable-linger $USER  so it runs without a login)
  macOS    launchd agent      ~/Library/LaunchAgents/com.paperbot.agent.plist
  Windows  Task Scheduler     "PaperBot" at log-on (your account, no admin);
           also removes the old HT_* tasks so only one scheduler runs

The service runs `python -m paperbot.cli start --yes` with this interpreter: no
menu (the saved strategy is used, so start once by hand first) and no
confirmation. A STOP file makes it exit cleanly without restarting. Live mode
needs --live-confirm ACCOUNT_ID (stored as PAPERBOT_LIVE_CONFIRM in the
service), otherwise installing is refused.
"""
import dataclasses
import subprocess
import sys
from pathlib import Path

from paperbot import paths

NAME = "paper-bot"
WINDOWS_TASK = "PaperBot"
MAC_LABEL = "com.paperbot.agent"


@dataclasses.dataclass
class Plan:
    system: str
    files: dict[Path, str]
    install: list[list[str]]
    uninstall: list[list[str]]
    status: list[list[str]]
    notes: list[str]


def start_command(python: Path | None = None) -> list[str]:
    return [str(python or paths.PYTHON), "-m", "paperbot.cli", "start", "--yes"]


def _systemd_unit(cmd: list[str], env: dict[str, str]) -> str:
    lines = ["[Unit]", "Description=paper-bot (IBKR trading bot)",
             "After=network-online.target", "Wants=network-online.target", "",
             "[Service]", "Type=simple", f"WorkingDirectory={paths.PROJECT_ROOT}",
             "ExecStart=" + " ".join(f'"{c}"' if " " in c else c for c in cmd),
             "Restart=on-failure", "RestartSec=60", "Environment=PYTHONUNBUFFERED=1"]
    lines += [f"Environment={k}={v}" for k, v in env.items()]
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _launchd_plist(cmd: list[str], env: dict[str, str]) -> str:
    args = "\n".join(f"    <string>{c}</string>" for c in cmd)
    envs = "".join(f"\n    <key>{k}</key><string>{v}</string>" for k, v in env.items())
    log = paths.LOGS_DIR / "service.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{MAC_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>WorkingDirectory</key><string>{paths.PROJECT_ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>{envs}
  </dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def plan(system: str | None = None, home: Path | None = None, python: Path | None = None,
         env: dict[str, str] | None = None) -> Plan:
    system = system or sys.platform
    home = home or Path.home()
    cmd, env = start_command(python), dict(env or {})
    if system.startswith("linux"):
        unit = home / ".config" / "systemd" / "user" / f"{NAME}.service"
        return Plan("linux (systemd user service)", {unit: _systemd_unit(cmd, env)},
                    [["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", NAME]],
                    [["systemctl", "--user", "disable", "--now", NAME]],
                    [["systemctl", "--user", "status", NAME, "--no-pager"]],
                    ["On a server, run once so it runs without a login: "
                     "sudo loginctl enable-linger $USER",
                     f"Logs: journalctl --user -u {NAME} -f  (and logs/bot.log)"])
    if system == "darwin":
        plist = home / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
        return Plan("macOS (launchd agent)", {plist: _launchd_plist(cmd, env)},
                    [["launchctl", "load", "-w", str(plist)]],
                    [["launchctl", "unload", "-w", str(plist)]],
                    [["launchctl", "list", MAC_LABEL]],
                    [f"Logs: logs/service.log and logs/bot.log"])
    if system == "win32":
        quoted = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        if env:  # schtasks cannot set variables: wrap in cmd /c set ...&& ...
            sets = " && ".join(f"set {k}={v}" for k, v in env.items())
            quoted = f'cmd /c "{sets} && {quoted}"'
        return Plan("Windows (Task Scheduler at log-on)", {},
                    [["schtasks", "/Create", "/TN", WINDOWS_TASK, "/SC", "ONLOGON", "/RL", "LIMITED",
                      "/TR", quoted, "/F"],
                     ["schtasks", "/Run", "/TN", WINDOWS_TASK]],
                    [["schtasks", "/End", "/TN", WINDOWS_TASK],
                     ["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"]],
                    [["schtasks", "/Query", "/TN", WINDOWS_TASK, "/V", "/FO", "LIST"]],
                    ["Runs while you are logged on, like the old HT_* tasks (which are removed).",
                     "Keep the PC awake: paper-bot holds off sleep only while it runs."])
    raise SystemExit(f"unsupported system {system!r}")


def _run(cmds: list[list[str]], dry_run: bool, runner) -> int:
    code = 0
    for cmd in cmds:
        print(("would run: " if dry_run else "run: ") + " ".join(cmd))
        if not dry_run:
            result = runner(cmd, check=False, capture_output=True, text=True, errors="replace")
            out = (result.stdout or result.stderr or "").strip()
            if out:
                print("  " + out.replace("\n", "\n  "))
            code = code or result.returncode
    return code


def install(dry_run: bool = False, live_confirm: str | None = None, runner=subprocess.run,
            system: str | None = None, home: Path | None = None) -> int:
    from paperbot.core import broker, selector
    from paperbot.core.risk import ConfigError
    import json
    try:
        key = selector.unattended_key()
        conn = broker.settings(json.loads(paths.CONFIG_FILE.read_text()))
    except ConfigError as e:
        print(f"Not installed: {e}")
        return 1
    env = {}
    if conn.live:
        if live_confirm != conn.account_id:
            print(f"Not installed: config.json is in LIVE mode; pass --live-confirm "
                  f"{conn.account_id} to let the service trade it unattended.")
            return 1
        env["PAPERBOT_LIVE_CONFIRM"] = conn.account_id
    p = plan(system, home, env=env)
    print(f"Installing paper-bot as {p.system}: strategy {key}, {conn.describe()}")
    for path, text in p.files.items():
        print(("would write: " if dry_run else "write: ") + str(path))
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if (system or sys.platform) == "win32" and not dry_run:
        from paperbot.ops import cleanup_schedule  # the old HT_* tasks would run a second scheduler
        if cleanup_schedule.ht_tasks():
            print("removing the old HT_* Task Scheduler tasks")
            cleanup_schedule.main()
    code = _run(p.install, dry_run, runner)
    for note in p.notes:
        print(f"note: {note}")
    return code


def uninstall(dry_run: bool = False, runner=subprocess.run, system: str | None = None,
              home: Path | None = None) -> int:
    p = plan(system, home)
    code = _run(p.uninstall, dry_run, runner)
    for path in p.files:
        if path.exists():
            print(("would remove: " if dry_run else "remove: ") + str(path))
            if not dry_run:
                path.unlink()
    return code


def status(runner=subprocess.run, system: str | None = None, home: Path | None = None) -> int:
    return _run(plan(system, home).status, False, runner)
