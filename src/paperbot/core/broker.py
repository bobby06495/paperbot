"""Broker connection settings and account checks for paper or live trading.

Switching to live is a configuration change only (config.json):

    "mode": "paper",                      <- "paper" or "live"
    "connections": {
      "paper": {"host": "127.0.0.1", "port": 4002, "client_id": 21},
      "live":  {"host": "127.0.0.1", "port": 4001, "client_id": 21,
                "account_id": "U0000000"}
    }

A config without "connections" is the original paper-only layout (host, port
and client_id at the top level) and runs in paper mode only.

Safety checks (fail closed, nothing connects or trades if one fails):
  * the port must belong to the mode (paper and live ports never overlap)
  * paper: IBKR must report exactly one account and it must be a paper
    account (id starting with "D")
  * live: config must name account_id, and IBKR must report exactly that
    account and nothing else
  * live start-up must be confirmed: interactively by typing the account id,
    or unattended (service, Docker) with PAPERBOT_LIVE_CONFIRM=<account_id>
PAPERBOT_IB_HOST / PAPERBOT_IB_PORT override host/port (e.g. in a container
next to an IB Gateway container); the port still has to match the mode.
"""
import dataclasses
import os
import re
from typing import Callable

from paperbot.core.risk import ConfigError

# IB Gateway / TWS API ports; 4004 / 4003 are the paper / live relays of the
# common IB Gateway Docker image (deploy/docker-compose.yml)
PAPER_PORTS = {4002, 7497, 4004}
LIVE_PORTS = {4001, 7496, 4003}
MODES = ("paper", "live")


@dataclasses.dataclass(frozen=True)
class Connection:
    mode: str
    host: str
    port: int
    client_id: int
    account_id: str | None = None  # live only: the one account the bot may trade

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def describe(self) -> str:
        account = f", account {self.account_id}" if self.account_id else ""
        return f"{self.mode.upper()} via {self.host}:{self.port} (clientId {self.client_id}{account})"


def settings(cfg: dict) -> Connection:
    """The connection for config.json's mode; ConfigError if anything is off."""
    mode = cfg.get("mode", "paper")
    if mode not in MODES:
        raise ConfigError(f'config.json mode must be "paper" or "live", got {mode!r}')
    connections = cfg.get("connections")
    if connections is None:  # original layout: paper only
        if mode == "live":
            raise ConfigError('config.json mode "live" needs a "connections.live" section')
        raw = {k: cfg.get(k) for k in ("host", "port", "client_id")}
    else:
        if not isinstance(connections, dict) or not isinstance(connections.get(mode), dict):
            raise ConfigError(f'config.json needs a "connections.{mode}" section for mode {mode!r}')
        raw = dict(connections[mode])
    where = f"config.json connections.{mode}" if connections is not None else "config.json"
    host = os.environ.get("PAPERBOT_IB_HOST") or raw.get("host")
    port = os.environ.get("PAPERBOT_IB_PORT") or raw.get("port")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: port must be a number, got {port!r}") from None
    client_id = raw.get("client_id")
    if not isinstance(host, str) or not host:
        raise ConfigError(f"{where}: host is missing")
    if not isinstance(client_id, int) or isinstance(client_id, bool):
        raise ConfigError(f"{where}: client_id must be a whole number, got {client_id!r}")
    allowed = LIVE_PORTS if mode == "live" else PAPER_PORTS
    if port not in allowed:
        raise ConfigError(f"{where}: port {port} is not a {mode} port {sorted(allowed)}")
    account_id = raw.get("account_id")
    if mode == "live":
        if not (isinstance(account_id, str) and account_id.strip()):
            raise ConfigError(f"{where}: live trading needs account_id (your live account, e.g. U0000000)")
        account_id = account_id.strip()
        if account_id.startswith("D"):
            raise ConfigError(f"{where}: account_id {account_id} is a paper account id")
        if not re.fullmatch(r"[A-Z]{1,3}\d{3,}", account_id):
            raise ConfigError(f"{where}: account_id {account_id!r} is not an IBKR account id; "
                              "set your live account (e.g. U0000000)")
    return Connection(mode, host, port, client_id, account_id if mode == "live" else None)


def verify_account(conn: Connection, accounts: list[str]) -> str:
    """The account to trade, or SystemExit if IBKR's accounts do not match the mode."""
    if conn.live:
        if accounts != [conn.account_id]:
            raise SystemExit(f"Refusing to run: live mode expects exactly account "
                             f"{conn.account_id}, IBKR reports {accounts}")
        return conn.account_id
    if conn.port not in PAPER_PORTS:
        raise SystemExit(f"Refusing to run: port {conn.port} is not a paper port {PAPER_PORTS}")
    if len(accounts) != 1 or not accounts[0].startswith("D"):
        raise SystemExit(f"Refusing to run: expected one paper account, got {accounts}")
    return accounts[0]


def confirm_live(conn: Connection, interactive: bool, ask: Callable[[str], str] = input,
                 say: Callable[[str], None] = print):
    """Live mode only: the account id typed by the user, or PAPERBOT_LIVE_CONFIRM set to
    it for unattended runs. SystemExit otherwise. Paper mode needs no confirmation."""
    if not conn.live:
        return
    from paperbot.core.common import log
    if os.environ.get("PAPERBOT_LIVE_CONFIRM", "").strip() == conn.account_id:
        log.warning("LIVE trading on %s confirmed by PAPERBOT_LIVE_CONFIRM", conn.account_id)
        return
    if interactive:
        say("")
        say(f"  *** LIVE TRADING with REAL MONEY on account {conn.account_id} ***")
        if ask("  Type the account id to confirm: ").strip() == conn.account_id:
            log.warning("LIVE trading on %s confirmed at the terminal", conn.account_id)
            return
        raise SystemExit("Live trading not confirmed; nothing started.")
    log.error("ALERT refusing to start LIVE trading unattended: set PAPERBOT_LIVE_CONFIRM=%s",
              conn.account_id)
    raise SystemExit("Refusing to start live trading unattended without PAPERBOT_LIVE_CONFIRM")
