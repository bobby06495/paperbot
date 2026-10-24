"""Choose the strategy for this session, before anything connects to IBKR.

    choose(cli_key, interactive=True)   the start-up path of `paper-bot start`

Precedence: --strategy KEY, else the interactive menu (when a person is at the
terminal), else the saved choice (logs/active_strategy.json, written by the
last start), else rules.json "default_strategy". The menu lists every strategy
in rules.json in file order, so a new section appears automatically; a section
that fails validation is listed with its error and cannot be chosen.

Switch guard: changing strategy is refused while the previously active one
still has positions in its saved state (they would be left unmanaged).
"""
import datetime as dt
import json
from typing import Callable

from paperbot import paths
from paperbot.core import profile as profiles
from paperbot.core.common import ET
from paperbot.core.risk import ConfigError
from paperbot.runtime import runstate

ACTIVE_FILE = paths.LOGS_DIR / "active_strategy.json"


def saved() -> str | None:
    try:
        data = runstate.read_json_state(ACTIVE_FILE) or {}
    except runstate.StateCorrupt:
        return None
    key = data.get("strategy")
    return key if isinstance(key, str) else None


def save(key: str):
    runstate.write_json_atomic(ACTIVE_FILE, {
        "strategy": key, "selected_at": dt.datetime.now(ET).isoformat(timespec="seconds")})


def unattended_key(rules_path=None) -> str:
    """The strategy for a run with nobody at the terminal (scheduler, service)."""
    keys = profiles.strategy_keys(rules_path)
    for key, source in ((saved(), str(ACTIVE_FILE.name)),
                        (profiles.default_strategy(rules_path), "rules.json default_strategy")):
        if key:
            if key not in keys:
                raise ConfigError(f"{source} names strategy {key!r}, which is not in rules.json "
                                  f"(available: {', '.join(keys)})")
            return key
    raise ConfigError("no strategy chosen: run `paper-bot start` once and pick one, pass "
                      "--strategy KEY, or set default_strategy in rules.json")


def open_positions(profile) -> list[str] | None:
    """Symbols the strategy still manages per its saved state; None if unreadable."""
    try:
        state = runstate.read_json_state(profile.state_file) or {}
    except runstate.StateCorrupt:
        return None
    return sorted(state.get("positions") or {})


def switch_guard(previous: str | None, new: str, loaded: dict):
    """Refuse to switch away from a strategy that still manages positions."""
    if not previous or previous == new or previous not in loaded:
        return
    prev = loaded[previous]
    if isinstance(prev, ConfigError):
        return  # an invalid strategy never ran with this configuration
    held = open_positions(prev)
    if held is None:
        raise ConfigError(f"cannot read {prev.label}'s state ({prev.state_file}); "
                          "resolve it before switching strategies")
    if held:
        raise ConfigError(f"{prev.label} still manages {', '.join(held)}; close them (or let its "
                          f"exits finish) before switching to another strategy")


def load_each(rules_path=None) -> dict:
    """{key: StrategyProfile or the ConfigError that made it invalid}, in menu order."""
    out = {}
    for key in profiles.strategy_keys(rules_path):
        try:
            out[key] = profiles.load(key, rules_path)
        except ConfigError as e:
            out[key] = e
    return out


def menu(loaded: dict, current: str | None, header: str, ask: Callable[[str], str] = input,
         say: Callable[[str], None] = print) -> str | None:
    """Numbered strategy menu; returns the chosen key, or None to quit."""
    keys = list(loaded)
    say("")
    say(f"  {header}")
    say("  " + "-" * len(header))
    for i, key in enumerate(keys, 1):
        p = loaded[key]
        if isinstance(p, ConfigError):
            say(f"  {i}) {key}  [INVALID: {p}]")
            continue
        mark = "   (current)" if key == current else ""
        say(f"  {i}) {p.label:<26} {p.summary}{mark}")
        say(f"     {p.menu_line()}")
    if current in loaded and not isinstance(loaded[current], ConfigError):
        held = open_positions(loaded[current])
        state = f"{len(held)} open position(s)" if held else "no open positions"
        say(f"  Current: {loaded[current].label} - {state if held is not None else 'state unreadable'}")
    hint = f"1-{len(keys)}" + (", Enter keeps current" if current in loaded else "") + ", q quits"
    while True:
        answer = ask(f"  Choice [{hint}]: ").strip().lower()
        if answer in ("q", "quit", "exit"):
            return None
        if answer == "" and current in loaded and not isinstance(loaded[current], ConfigError):
            return current
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            key = keys[int(answer) - 1]
            if isinstance(loaded[key], ConfigError):
                say(f"  {key} is invalid and cannot be started: {loaded[key]}")
                continue
            return key
        say(f"  Please enter a number from 1 to {len(keys)}, or q.")


def choose(cli_key: str | None = None, interactive: bool = True, assume_yes: bool = False,
           mode_text: str = "PAPER", rules_path=None, ask: Callable[[str], str] = input,
           say: Callable[[str], None] = print):
    """The profile to run this session, validated and saved; SystemExit if the user
    quits or anything is invalid (logged as an ALERT when unattended)."""
    from paperbot.core.common import log
    try:
        loaded = load_each(rules_path)
        previous = saved()
        if cli_key:
            if cli_key not in loaded:
                raise ConfigError(f"no strategy {cli_key!r} in rules.json "
                                  f"(available: {', '.join(loaded)})")
            key = cli_key
        elif interactive:
            key = menu(loaded, previous or profiles.default_strategy(rules_path),
                       f"PAPER-BOT - choose today's strategy  |  mode {mode_text}", ask, say)
            if key is None:
                raise SystemExit("No strategy chosen; nothing started.")
        else:
            key = unattended_key(rules_path)
        chosen = loaded[key]
        if isinstance(chosen, ConfigError):
            raise chosen
        switch_guard(previous, key, loaded)
        if interactive and not assume_yes:
            say("")
            for line in chosen.describe():
                say(f"  {line}")
            if ask(f"  Start {chosen.label} in {mode_text} mode? [y/N]: ").strip().lower() not in ("y", "yes"):
                raise SystemExit("Not confirmed; nothing started.")
    except ConfigError as e:
        log.error("ALERT refusing to start: %s", e)
        raise SystemExit(f"Refusing to start: {e}") from None
    save(key)
    return chosen


def describe_all(rules_path=None) -> list[str]:
    """`paper-bot strategies`: every strategy with its status."""
    lines, current = [], saved()
    for key, p in load_each(rules_path).items():
        if isinstance(p, ConfigError):
            lines.append(f"{key}: INVALID - {p}")
            continue
        mark = "  (active)" if key == current else ""
        lines.extend([f"{key}{mark}", *(f"  {line}" for line in p.describe()), ""])
    return lines


def to_json(profile) -> str:
    return json.dumps({"strategy": profile.key, "label": profile.label})
