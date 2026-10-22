# paperbot

A rules-driven, long-only trading bot for **Interactive Brokers** equities and ETFs.
Strategies are sections of `config/rules.json`; you pick one each session from a menu.
It runs the same way on **Windows, macOS, Linux, servers and Docker** with one command,
`paper-bot start`, and talks to **IB Gateway** through
[`ib_async`](https://github.com/ib-api-reloaded/ib_async).

Every order passes a risk guard first, every entry carries a **protective stop held at
IBKR**, and the account it trades must match the configured mode: **paper** by default,
**live** only after a configuration change plus an explicit confirmation.

---

## Quick start

```bash
# 1. install (any OS; Python 3.11+)
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip ...
.venv/bin/pip install --no-deps -e .

# 2. start IB Gateway, log in to the PAPER account (API port 4002), then:
paper-bot check           # offline setup check
paper-bot start           # strategy menu -> confirmation -> the whole trading day
```

```
  PAPER-BOT - choose today's strategy  |  mode PAPER
  ---------------------------------------------------
  1) Trend Join Long            Intraday stock breakouts - large-cap momentum   (current)
     5m | entries 10:05-15:30 | flat 15:51 | continuous/scheduled
  2) Macro Swing ETF            Multi-day sector pullbacks - holds positions overnight
     1d | entries 15:45-15:55 | holds overnight | continuous/scheduled
  3) Equities Mean Reversion    Intraday index ETF scalps on VWAP deviations
     1m | entries 10:00-15:30 | flat 15:50 | continuous
  Current: Trend Join Long - no open positions
  Choice [1-3, Enter keeps current, q quits]:
```

`paper-bot start` keeps running and does the whole trading day by itself: a strategy pass
every 30 s, the morning watchlist, log rotation and the daily summary (the **built-in
scheduler**), and it reconnects on its own when IB Gateway restarts or the network drops.
Stop it with `Ctrl+C` or `paper-bot stop` (protective stops always stay at IBKR).

| Command | Does |
|---|---|
| `paper-bot start [--strategy KEY] [--yes] [--dry-run]` | choose (menu, or `--strategy`), confirm (`--yes` skips), run; `--dry-run` logs orders instead of sending |
| `paper-bot stop` · `paper-bot kill [--all]` | graceful stop · cancel the bot's entries/exits (`--all`: every order) |
| `paper-bot status` | mode, strategy, process, last pass, positions, scheduled jobs |
| `paper-bot strategies` | every strategy in `rules.json` with all its settings (or why it is invalid) |
| `paper-bot check` · `paper-bot test` | setup check · unit tests |
| `paper-bot dashboard [--open]` | rebuild (and open) the monitoring dashboard |
| `paper-bot service install / uninstall / status` | start automatically at boot or log-on (see *Run unattended*) |
| `paperbot-backtest run [--strategy KEY]` · `all [--with-experiments]` | historical backtest of any strategy (never trades; see *Backtesting*) |

`paperbot` is the same command; the older `paperbot-*` commands still work.

Before it connects, every start (and every scheduled run) logs a `PREFLIGHT` summary:
- the connection;
- the strategy, with a short SHA-256 of `rules.json`;
- the timeframe, window, universe and blocks;
- every effective risk limit.

It refuses to start, with an ALERT, if the selected strategy, the loaded profile, the risk
guard and `rules.json` on disk do not all agree.

---

## Project layout

```
paperbot/
├─ pyproject.toml          package metadata, dependencies, the paper-bot command
├─ requirements.txt        exact dependency versions this project is tested with
├─ .env.example            copy to .env and fill in (Telegram); .env is never committed
├─ config/                 config.json · rules.json · universe.txt              (tracked)
├─ src/paperbot/
│  ├─ cli.py               the paper-bot command
│  ├─ core/                the runner, no strategy knowledge: engine (session loop, orders),
│  │                       clock (built-in scheduler), selector (menu), profile (rules.json),
│  │                       broker (paper/live), risk, sizing, orders, market_data, account
│  ├─ strategies/          rule_strategy: the one generic strategy that runs any profile
│  ├─ blocks/              named building blocks: entry checks, stop rules, manage, exit
│  ├─ indicators/          pure functions: sma, swing_low, relative_volume
│  ├─ system/              the only OS-specific code: process checks, keep-awake, services
│  ├─ runtime/             runstate (STOP/pid/state files) · stopbot · kill_switch
│  ├─ market/              sp500_tickers · morning_prefilter
│  ├─ reporting/           notify · compute_perf · telemetry · dashboard (+ web/ pages)
│  ├─ backtesting/         historical simulator driving the real strategy engine (paperbot-backtest)
│  └─ ops/                 check_setup · rotate_logs · run_tests (+ legacy Windows scheduling)
├─ deploy/                 Dockerfile, docker-compose.yml (bot + headless IB Gateway)
├─ scripts/                Windows double-click launchers (= paper-bot start/stop), manual/
├─ tests/                  offline unit tests (no Gateway, no network)
├─ backtesting/            config/ + experiments/ (tracked); results/ reports/ data_cache/ (ignored)
└─ data/ logs/ dashboard/ STOP      runtime output, all git-ignored
```

| Path | Contents |
|---|---|
| `config/config.json` | `mode` (paper/live), `connections` per mode, loop timing, safety rails (`limits`: allowed symbols, limit orders only), `fx_fallback` |
| `config/rules.json` | `strategies`: one section per strategy (blocks, timeframe, hours, universe, risk) and `default_strategy` |
| `config/universe.txt` | Static fallback symbols, one IBKR ticker per line (`#` comments). Share classes use a space: `BRK B` |
| `config/universe_nasdaq100.txt` | Static fallback for a Nasdaq-100 screen: the 101 members on 2026-10-20, largest first |
| `data/watchlist.txt` | Today's top gappers, written by the prefilter (Trend Join Long's universe) |
| `data/trades.csv` | Fill ledger (`data/live/trades.csv` in live mode) |
| `.env` | Secrets only: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `NOTIFY_URL` |
| `logs/` | `bot.log`, `orders.jsonl`, state files, `heartbeat.json`, `scheduler_state.json`, `active_strategy.json`, `archive/` (live mode: `logs/live/`) |
| `dashboard/` | Monitoring pages, rebuilt after every pass (live mode: `dashboard/live/`) |
| `backtesting/config/backtest.json` | Backtest assumptions: capital, data provider, spread/slippage, volume cap, validation split (tracked) |
| `backtesting/experiments/EXP-*.json` | Rule variants per strategy to test; never applied to `rules.json` (tracked) |
| `backtesting/results/`, `reports/`, `data_cache/` | Backtest runs, HTML reports and downloaded market data (git-ignored) |

`src/paperbot/paths.py` is the single place that knows where files are, so every command
works from any folder, service or container.

---

## 1. Install and initialize

Requirements: **Python 3.11+** and **IB Gateway** (or TWS). Create a virtual environment
anywhere (this PC uses `C:\Users\User\IBKR\.venv`, one level above the project):

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python.exe
.venv/bin/python -m pip install --no-deps -e .
```

The last line installs paperbot in *editable* mode: the code stays here and `paper-bot`
appears in the venv's `bin/` (`Scripts\` on Windows). Activate the venv in each new
terminal, or call it by path. Then create your secrets file: `cp .env.example .env`.

On Windows, `scripts\start_paperbot.ps1` (and the double-click `start_paperbot.cmd`) uses
`-Python <path>`, else `PAPERBOT_PYTHON`, else the one `.venv` found in the project or its
parent folder. If **both** exist, it stops and asks you to choose. It prints the interpreter
and refuses to start unless that interpreter imports paperbot from this project.

### IB Gateway settings (Configure → Settings → API → Settings)
| Setting | Value |
|---|---|
| Enable ActiveX and Socket Clients | on |
| Read-Only API | **off** (the bot must place orders) |
| Socket port | **4002** paper (4001 live: used only in live mode) |
| Trusted IPs | 127.0.0.1 |

### Check the setup (offline, never connects)
```bash
paper-bot check      # config, every rules.json strategy, state files, notifications
paper-bot test       # unit tests; results to logs/test_results.json + the dashboard
```

---

## 2. Strategies: pick one per session, add new ones in rules.json

Each section of `rules.json → strategies` is a complete strategy, assembled from named
**blocks**. Selecting it loads all of its settings before anything connects: timeframe,
entry window and force-close time, universe, entry checks, stop rule, position
management, exits and risk limits.

For a beginner-friendly, code-grounded explanation of every configured strategy, worked
examples, execution flow, and corner cases, see [docs/STRATEGY_GUIDE.md](docs/STRATEGY_GUIDE.md).

```json
{ "default_strategy": "trend_join_long",
  "strategies": {
    "trend_join_long": {
      "label": "Trend Join Long", "summary": "Intraday stock breakouts - large-cap momentum",
      "timeframe": "5m", "holding": "intraday",
      "universe": {"source": "watchlist", "fallback_file": "universe.txt"},
      "time_filter": {"earliest_entry_et": "10:05", "latest_entry_et": "15:30", "force_close_et": "15:51"},
      "entry": [{"type": "prior_close_above_sma", "label": "D2", "period": 200},
                {"type": "min_gap_pct", "label": "D3", "min_pct": 3.0}, "..."],
      "stop":   {"type": "low_of_day_minus_pct", "pct": 1.0},
      "manage": [{"type": "partial_profit", "at_R": 0.75, "fraction": 0.3333},
                 {"type": "breakeven", "at_R": 1.0}, {"type": "trail_swing_low", "left": 2, "right": 2}],
      "exit":   [{"type": "force_close"}],
      "risk":   {"max_risk_per_trade_pct": 1.0, "max_order_value": 30000.0, "...": "..."} } } }
```

| Strategy | What it trades | Entry | Stop | Exit |
|---|---|---|---|---|
| `trend_join_long` | today's S&P 500 gappers, 5m bars, flat every day | breakout above prior-day/premarket/day high, gap ≥ 3 %, RVOL ≥ 2, above SMA200 (10:05–15:30) | day's low − 1 % | ⅓ at +0.75R, breakeven at +1R, swing-low trail, force close 15:51 |
| `macro_swing_etf` | 11 SPDR sector ETFs, daily bars, **held overnight** | price > SMA200 and RSI(2) < 10, using the live price as today's close (15:45–15:55) | 2 × ATR(14), GTC | at 15:50: price > SMA(5), or 10 trading days held |
| `equity_mean_reversion` | SPY, QQQ, IWM, 1-minute bars, flat every day, **continuous mode only**, up to 3 entries per symbol per day | last completed 1m close ≤ session VWAP − 2 sd (10:00–15:30) | 1 sd below the signal bar's close | back at VWAP, 30 minutes held, force close 15:50 |

**Switching the index (e.g. to QQQ's Nasdaq-100 stocks).** A strategy whose universe is the
morning watchlist names its index in `rules.json`:
```json
"universe": {"source": "watchlist", "index": "Nasdaq-100", "top_n": 101,
             "fallback_file": "universe_nasdaq100.txt"}
```
| Key | Values |
|---|---|
| `index` | `"S&P 500"` (default) or `"Nasdaq-100"`; anything else stops the bot and the prefilter with an error naming the key |
| `top_n` | how many of the largest members (by market cap) the prefilter screens (default 100; the Nasdaq-100 has 101 lines, so 101 screens all) |
| `fallback_file` | the `config/` list scanned when there is no watchlist for today's index (default `universe.txt`; `universe_nasdaq100.txt` ships with the Nasdaq-100 members) |

The prefilter's gap and price thresholds are the strategy's own `min_gap_pct` and `min_price`
blocks, so the screen and the entry checks always agree; change them in that one place
(`paperbot-prefilter --min-gap / --min-price` override them for one run). The watchlist header
records its index (`# Index: Nasdaq-100`): after a switch, the bot ignores a list built for the
other index and scans the fallback file until the next prefilter run. `paper-bot check` shows
what the prefilter will screen. Refresh `universe_nasdaq100.txt` when the index is rebalanced.

Mean Reversion's own risk block allows fees up to **5 %** of R (`max_commission_pct_of_risk`):
scalps with ~0.15 % stops on $500+ ETFs cannot keep fees under 1 % of R at a $30k order cap.
Its per-trade risk is half the others' (0.5 %), with at most 2 positions.

**Daily (`1d`) strategies** evaluate the completed daily bars plus today's **live mid price
as the close**, near the end of the session; their entries expire one minute after the
window. **Multi-day holding** (`"holding": "multi_day"`, `force_close_et: null`) keeps a
position overnight on its GTC stop until an exit block fires. Exits never fire on the entry
day, and a decided exit is kept until it fills, through the next session if needed. Every
exit is a marketable SELL in the stop's OCA group, so the stop stays live until the SELL fills.

**Adding a strategy.** Copy a section, give it a new key and label, and change its blocks
and settings. It appears in the menu automatically; `paper-bot strategies` shows it, and
`paper-bot check` validates it. A typo stops start-up with the exact place, e.g.
`strategies.x.entry[1]: unknown entry type "rsi_bellow"; valid: ...`. Then add its limits
to `APPROVED_LIMITS` in `tests/test_risk_policy.py` (the reviewed safety lock on every
strategy's limits). Each strategy gets its own order tags and state folder, so strategies
never touch each other's orders.

| Kind | Blocks available | Settings |
|---|---|---|
| entry | `min_prior_close`, `min_price` | `min_usd` |
| | `prior_close_above_sma` | `period` (200) |
| | `close_above_sma` | `period`: today's price above the SMA of daily closes |
| | `rsi_below` | `period` (2), `max`: Wilder RSI of daily closes |
| | `above_prior_day_high` | none |
| | `above_premarket_high`, `new_high_of_day` * | none |
| | `min_gap_pct` * | `min_pct` |
| | `min_rvol` * | `min`, `lookback_days` (14) |
| | `below_vwap_sigma` * | `sigma`: last close ≤ session VWAP − sigma × sd |
| stop | `low_of_day_minus_pct` * | `pct` |
| | `atr_multiple` | `period` (14), `multiple`: below today's price |
| | `sigma_below_entry` * | `sigma` × the session VWAP sd below the entry price |
| manage | `partial_profit` * | `at_R`, `fraction` (< 1) |
| | `breakeven` * | `at_R` |
| | `trail_swing_low` * | `left` (2), `right` (2) |
| exit | `force_close` * | none (uses `time_filter.force_close_et`; intraday holding) |
| | `close_above_sma` | `period`, optional `at_et` ("HH:MM") |
| | `time_stop` | `days` (trading days) or `minutes`, optional `at_et` |
| | `target_vwap` * | none: exit when the live price is back at the session VWAP |

\* needs intraday bars: not allowed on a `1d` strategy (start-up refuses it).

Every block also takes an optional `label` (e.g. `"D3"`) that prefixes its log reasons.
Section keys: `label`, `summary`, `timeframe` (`1m`, `5m`, `15m`, `1h`, `1d`), `holding`
(`intraday` or `multi_day`), `universe` (`{"source": "watchlist"}`, `{"symbols": [...]}` or `{"file": "x.txt"}`),
`time_filter`, `entry`, `stop`, `manage`, `exit`, `risk`, plus optional `order_ref`,
`modes` (`["continuous"]` refuses the legacy 5-minute scheduled runs), `entry_reason`,
`state_dir`, `max_entries_per_symbol_per_day` (default 1; re-entries after an exit). A new **kind** of signal is one small block class in
`src/paperbot/blocks/` (about 20–40 lines, `@register("entry", "name")`); the engine, menu,
sizing and risk code need no change.

**Switching strategy** is refused while the previous one still manages positions (they
would be left unmanaged); finish or close them first.

**Sizing and rounding** are the same for every strategy (`core/sizing.py`): whole shares,
the smallest of 1 % risk, 10 % position, the share and order-value caps and buying power,
then the commission check. Prices go to the $0.01 tick in the safe direction (BUY limits
up, SELL limits and long stops down).

---

## Backtesting

*Historical simulation results do not guarantee future performance.*

`paperbot-backtest` replays **any strategy in `rules.json`** over historical data and **never
sends an order**. It runs the bot's real strategy engine: every simulated pass calls
`rule_strategy.decide()`, so the same entry blocks, stop rules, management (breakeven, trail,
partial profit), exits, sizing and live risk guard decide. Only IBKR is simulated (bars, quotes,
fills, positions, the clock).

```bash
paperbot-backtest strategies                          # what can be backtested (all of rules.json)
paperbot-backtest run --strategy trend_join_long      # one strategy, last 120 days, IBKR data
paperbot-backtest run --strategy macro_swing_etf --provider yfinance   # no Gateway needed
paperbot-backtest all                                 # every strategy (baseline)
paperbot-backtest propose                             # experiment ideas per strategy -> backtesting/experiments/
paperbot-backtest all --with-experiments              # baselines + every experiment + comparisons
paperbot-backtest experiment EXP-trend_join_long-rvol-lower
paperbot-backtest compare <run_id_A> <run_id_B>
paperbot-backtest list · report [--period weekly|monthly] · prune --keep 50 --dry-run
```

| Where | What |
|---|---|
| `dashboard/backtest.html` (top-bar **BACKTESTING**) | Overview, strategy reports, portfolio, trades, performance, risk, rule experiments, data quality, run comparison, limitations |
| `backtesting/results/<strategy>/<run_id>/` | One folder per run, never overwritten: metadata, config snapshot, trades, orders, fills, equity, daily/monthly results, metrics, validation, `report.html` |
| `backtesting/reports/` | latest report per strategy, portfolio summary, weekly/monthly reports |

| Data provider | Needs | History |
|---|---|---|
| `ibkr` (default) | IB Gateway logged in to the **paper** account | years; read-only connection, client id `client_id + 3`; live ports and accounts refused |
| `yfinance` (automatic fallback) | internet | ~30 days of 1m, ~60 days of 5m bars: too short for conclusions |
| `synthetic` | nothing | generated prices for tests/demos, labelled **SYNTHETIC** everywhere |

Safety: every backtest process sets `PAPERBOT_BACKTEST=1` (and `PAPERBOT_NOTIFY=off`); `paper-bot`,
`paperbot-bot`, `paperbot-cycle` and the order functions in `core/engine.py` refuse to run under it.
`config/rules.json` is never written (hashed before and after each run); rule changes found by
experiments must be reviewed and copied in by hand. Backtests are manual only (no scheduled job).
Full guide: [backtesting/README.md](backtesting/README.md).

---

## 3. Paper or live: configuration only

```json
"mode": "paper",
"connections": {
  "paper": {"host": "127.0.0.1", "port": 4002, "client_id": 21},
  "live":  {"host": "127.0.0.1", "port": 4001, "client_id": 21, "account_id": "U0000000"}
}
```

Change `"mode"` to `"live"` and set your live `account_id`. No code changes. The bot then:
- accepts only live ports (4001/7496) and exactly the configured account; paper only
  paper ports (4002/7497) and one `D…` account. Any mismatch refuses to start.
- asks you to **type the account id** at start-up. A service or container needs
  `PAPERBOT_LIVE_CONFIRM=<account_id>` instead; without it, live refuses to start.
- keeps its own state, logs, ledger and dashboard (`logs/live/`, `data/live/`,
  `dashboard/live/`), prefixes every push with `[LIVE]`, and the dashboard says LIVE.
- uses the same risk engine, limits, drawdown breaker and approved-limits test.

In live, real market-data subscriptions and commissions apply, and US accounts under
$25k are limited to 3 day trades per 5 days (pattern day trader rule). The intraday
strategies day-trade every position.

---

## 4. A trading day

The bot refuses to start if the config or a strategy is invalid, the account does not
match the mode, a state file in `logs/` is corrupt (see *Recovery*), or another bot is
running (`logs/bot.pid`). A `STOP` file left by `paper-bot stop` is cleared when you run
`paper-bot start` yourself; a service respects it and stays stopped.

**Each pass (every `loop_seconds`, default 30 s), for the chosen strategy:**
1. **Protects positions (any hour).** Each of its positions has a SELL STP (GTC) at IBKR,
   sized to the shares not covered by a working exit. A missing stop is re-created (ALERT);
   a stop without a position is cancelled.
2. **Manages exits (regular hours):** its `manage` blocks: breakeven, swing-low trail, partial
   profit (retried if unfilled).
3. **Force-closes** from `force_close_et`: a marketable SELL in the stop's OCA group, re-priced
   until flat; ALERTs if still open at 15:58 or after the close.
4. **Looks for entries** in its entry window: its `entry` blocks on its universe, once per bar,
   priced from live quotes. Each entry is a bracket (BUY + protective STP), expiring at the end
   of the window.

**Built-in scheduler** (weekdays, US Eastern; daylight saving handled automatically):

| Job | ET | Runs |
|---|---|---|
| `rotate_logs` | 09:25 | archive earlier days' logs, cap the safety log, trim `trades.csv` to 90 days |
| `prefilter` | 09:55, 10:25 … 12:55 | the morning gapper watchlist (only for strategies whose universe is the watchlist) |
| `daily_summary` | 16:05 | performance summary + push, dashboard |

A slot that was missed because the bot started late still runs if it is within its grace
period. Status in `paper-bot status`, `logs/scheduler_state.json` and the dashboard.

**Single-symbol check** (read-only connection with its own client id; safe while the bot runs):
```bash
paperbot-bot --symbol NVDA --check-only [--strategy KEY]
```

**Dry run first:** `paper-bot start --dry-run` connects and logs what it *would* send.

### Account currency
All sizing and limits are in USD. IBKR reports account values in the account's base currency
(a paper account inherits it from the live account). `src/paperbot/core/account.py` decides
the USD net liquidation the bot uses:
1. IBKR's USD value when it reports one (a USD-based account): used as is.
2. Otherwise, with `"fx_fallback": "auto"` in `config.json` (default), the base-currency value
   converted at IBKR's own ExchangeRate if reported, else Yahoo Finance (`SGD=X`, fetched once per
   day, saved in `logs/fx_rate.json`), else that saved rate if at most 5 days old.
3. Otherwise no value: the entry scan is skipped and entries are blocked, with one ALERT a day.

**Daily P&L while flat.** IBKR sends no daily P&L while the account has nothing to report, and
the risk guard blocks entries without one. `account.daily_pnl()` therefore uses 0.0 when the
account provably has no P&L: no position and no execution today.

### Limits (each key lives in exactly one file)
- `config.json → limits` (safety rails): allowed symbols (`["*"]` = any), limit orders only.
- `rules.json → strategies.<key>.risk` (thresholds): max shares and $ value per order, max
  entries per day, max working entry/exit orders (stops not counted), daily loss halt, max 1 %
  of the account at risk per trade, a position no bigger than 10 % of the account, concurrent
  positions, commission drag, multi-day drawdown.
- Every value is type-checked at startup; a missing, invalid or misplaced key stops the bot with
  an ALERT naming it. Exits and stops are never blocked by these limits, and can never be larger
  than the position, so the bot can never go short.

---

## 5. Run unattended (boot / log-on, servers, cloud)

Start once by hand (`paper-bot start`, choose the strategy), then:

```bash
paper-bot service install            # --dry-run shows exactly what it would do
paper-bot service status
paper-bot service uninstall
```

| OS | Installs | Notes |
|---|---|---|
| Linux | systemd user unit `~/.config/systemd/user/paper-bot.service` (restart on failure) | on a server also run `sudo loginctl enable-linger $USER` |
| macOS | launchd agent `~/Library/LaunchAgents/com.paperbot.agent.plist` | logs to `logs/service.log` |
| Windows | Task Scheduler task `PaperBot` at log-on (your account, no admin) | removes the old `HT_*` tasks |

The service runs `paper-bot start --yes` without a menu, using the saved strategy. Live mode
needs `paper-bot service install --live-confirm <account_id>`. While it runs, the bot keeps the
computer from sleeping (Windows/macOS) without changing your power settings.

**Docker / cloud VM** (bot + a headless IB Gateway that logs in by itself):
```bash
cp deploy/docker.env.example deploy/.env      # IBKR login, strategy, optional Telegram
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec paper-bot paper-bot status
```
Live logins still need IBKR's two-factor approval on your phone; no software can skip that.

**Legacy Windows scheduling.** `paperbot-schedule` / `paperbot-cycle` (the `HT_*` Task Scheduler
tasks running one pass every 5 minutes) still work but are no longer needed: `paper-bot start`
does the same in one process on every OS. Use one or the other, not both
(`paper-bot service install` on Windows removes the old tasks; `paperbot-unschedule` does too).

---

## 6. Stop the project gracefully

| Command | What happens to orders | Use it when |
|---|---|---|
| `paper-bot stop` | The bot cancels its working entries/exits, **keeps protective stops**, exits. Waits up to 120 s and reports. Never kills a process. | Normal shutdown |
| Create a file named `STOP` in the project folder | Same as above, on the next pass | No terminal handy |
| `Ctrl+C` in the bot window | Exits immediately and leaves **every** order in place (stops included) | You want it gone now |
| `paper-bot kill` | Creates `STOP`. If the bot isn't running, cancels its entries/exits itself. **Keeps stops.** | Stop and make sure no new entries fill |
| `paper-bot kill --all` | Creates `STOP`, then a **global cancel** of every order. **Positions become unprotected.** | Emergency only |

Open positions keep their GTC stop at IBKR while the bot is off; on the next start the bot
re-adopts them and re-creates any missing stop. Stops are held by IBKR's servers, not by
Gateway, so you can shut Gateway down too.

---

## Push notifications (Telegram, ntfy fallback)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Create the token with @BotFather,
then send your bot one message so it is allowed to write to you. Optionally add
`NOTIFY_URL=https://ntfy.sh/<your-topic>`; it is used only when Telegram isn't configured or
a Telegram send fails. Send yourself a test message:
```bash
python -c "from paperbot.reporting.notify import notify; notify('Test', 'Hello this is paperbot')"
```

| Message | When | Priority |
|---|---|---|
| `BUY X` | An entry fills: price, stop, quantity | default |
| `STOP X` | A protective stop fills: exit price, realized P&L | default |
| `PARTIAL X` | The partial-profit sell fills: sold/total @ price | default |
| `EXIT X` | A force-close sell fills: price, realized P&L | default |
| `BE X` / `TRAIL X` | The stop moves to breakeven / up to a new swing low | default |
| `EOD Force Close` | First force-close attempt, with the number of positions | high |
| `ALERT` / `ERROR` | Any `ALERT` log line or error, e.g. no protective stop, still open at 15:58, cannot connect. The same line is sent at most once per 10 minutes. | high |
| `Bot CRASHED` / `Cycle CRASHED` | An uncaught error in the bot / a legacy scheduled run | high |
| `[LIVE] …` | Every message from a bot in live mode starts with `[LIVE]` | — |
| `Prefilter …` | The prefilter (not `--dry-run`): survivors, failure, or degradation alert | default / high |

Fill messages come from IBKR's execution report, read every bot loop or cycle run. A stop
that fills while nothing is running is still reported, on the next run. Each fill is sent
once (`logs/notify_state.json`). Sending happens in the background with a 5-second timeout, so
it never delays trading. Problems are written to `logs/notify_errors.log` with the token
masked. When IB Gateway is unreachable the bot sends one ALERT per outage and keeps
retrying (30 s, doubling up to 5 min). Set `PAPERBOT_NOTIFY=off` to silence
everything (the test suite does this).

---

## Daily summary, dashboard and log rotation

**Trade ledger:** every fill of the bot's own orders is appended once to `data/trades.csv`
(`timestamp_iso` in ET, symbol, side, size, fill_price, order_id, status, order_ref, exec_id,
commission, stop_price; the initial stop is recorded on entry rows). It is written next to the
fill notifications, so it works with the bot and with scheduled runs, including stops that
fill between runs.

**`paperbot-perf`** (the built-in scheduler runs it at 16:05 ET) reads the ledger and never writes to it or connects to IBKR. It prints a JSON summary
and pushes it to Telegram:
```bash
paperbot-perf                    # today (ET)
paperbot-perf --date 2026-10-19  # another day
paperbot-perf --no-notify        # print only
```
A position sold in pieces (the 1/3 partial, then the rest) is **one** round trip. P&L is net
of commissions. Metrics: total_trades, wins, losses, win_rate_pct, gross_pnl_usd,
largest_winner/loser, avg_winner/loser, profit_factor (`inf` with no losers, `n/a` with no
trades). It also reports sells of earlier-day positions and lots still open, without counting them.

**Monitoring dashboard: `dashboard/index.html`.** Open it straight from disk (double-click);
no server is needed. It is a passive reporting layer: it never sends orders, never talks to
IBKR and never changes the scheduler.

| Page | Shows |
|---|---|
| `index.html` Overview | Runs today, current P&L vs the loss limit, active positions, system health; a **live state monitor** (what Paperbot is doing now, the HT_Cycle task's last result, a countdown to the next run and what it will do, trigger prices for each position); session timeline, order lifecycle, alerts, all `HT_*` tasks |
| `logs.html` Run log | Every run today (plus skipped runs and failed starts) with four expandable steps: market scan fetch, validations, signal evaluation, order events; the full order lifecycle (placed, modified, partial fills, cancels, liquidations) |
| `strategy.html` Strategy & scanner | Active parameters, the entry-filter funnel, exit ladder, risk limits, a sizing what-if, and the **market-scanning view**: each watched symbol's last evaluated price, the check that stopped it and how far it is from passing |
| `performance.html` | Trade performance (below), written by `compute_perf.py` |
| `backtest.html` + `bt-*.html` | **BACKTESTING**: overview, strategy reports, portfolio, trades, performance, risk, rule experiments, data quality, run comparison, limitations (data from `paperbot-backtest`) |

How it works:
- `paperbot.reporting.telemetry` appends one line per pass to `logs/telemetry.jsonl`, from
  `bot.after_pass()`. It only reads ib_async's cached data and the lines the bot already wrote to
  `bot.log`, `safety_log.jsonl` and `orders.jsonl`. A telemetry error is logged and trading continues.
- `paperbot.reporting.dashboard` turns that, plus `bot.log`, `trades.csv`, the state files and
  `schtasks /Query` for the `HT_*` tasks, into `dashboard/data/telemetry.js`. The pages load it
  with a script tag, reload it every 15 s, and recompute the phase and countdowns every second.
- Rebuild at any time (read-only, safe outside market hours):
  `python -m paperbot.reporting.dashboard`.
- Tailwind CSS and fonts come from CDNs. Offline, the pages still show all data with basic styling.
- Prices on the scanner are the last completed bar closes the bot evaluated (limit prices use live
  quotes). US market holidays are not modelled in the countdowns.

**Performance page: `dashboard/performance.html`.** It reloads itself every 30 s, and the bot and
scheduled runs rebuild it after every pass. `paperbot-perf` also writes it, together with the
monitoring pages (`--no-html` skips both).
- **Status strip** from `logs/heartbeat.json`:
  - **ACTIVE**: a pass within 10 min during 09:30–16:05 ET.
  - **STALE**: no pass for over 10 min during the session.
  - **IDLE**: market closed.
  - **CRASHED**: the last run crashed, with the error.
  - **NO DATA**: no heartbeat yet ("no cycle data yet").
- **Today:** P&L, wins/losses, win rate, profit factor.
- **R-multiple distribution** of all closed trades in the ledger, as pure-CSS bars in
  buckets ≤ −2R … > +3R. R = (exit − entry) / (entry − initial stop), using the stop recorded
  on the entry fill, i.e. the dollars actually risked. Without one, a stop 1 % below the entry
  is assumed and the R is marked *(est.)*.
- **Open positions:** qty, entry, current stop, last price and unrealized R, from the heartbeat.
  Positions the bot didn't open are marked *(manual)*.
- **Last 20 closed trades,** with R in green or red.

The page uses Bootstrap 5 CSS from a CDN and no JavaScript. Without internet it still shows
all the data, just unstyled.

**`paperbot-rotate`** (the built-in scheduler runs it at 09:25 ET, inside the bot):
- It moves every `logs/*.log` / `logs/*.jsonl` last written before today (ET) into
  `logs/archive/YYYY-MM-DD/`. A file written today stays until the next run.
- `logs/safety_log.jsonl` over 5 MB goes to `logs/archive/YYYY-MM-DD_safety_log.jsonl` and
  a fresh empty file starts.
- `data/trades.csv` keeps 90 days. Older rows are appended to
  `logs/archive/trades_YYYYMMDD.csv` and the file is rewritten atomically. Run by hand, it is
  skipped while a bot is active; the bot's own scheduled run does it safely.
- It uses `os.replace` only, with no copy-then-delete. A file another process has open is
  skipped and reported, never forced. State files (`*.json`), `bot.pid` and the archive are
  never touched. It prints `Rotated N files to logs/archive/` and always exits 0.

---

## Recovery

**Corrupt state file.** The bot stops with a message naming the file instead of silently
resetting today's order count or loss halt. Inspect the file, then run:
```bash
paperbot-bot --reset-state
```
This moves only the corrupt file(s) aside (`*.corrupt-<timestamp>`). If the strategy state
was reset, positions with a working paperbot stop are re-adopted from that stop (with no
further partial profit). Other positions are reported and left alone.

**Drawdown breaker tripped.** Entries stay blocked across days until you re-arm it (with the
bot stopped):
```bash
paperbot-bot --reset-drawdown
```
The current cumulative P&L becomes the new peak. Exits and stops are never blocked.

**Bot crashed.** A stale `logs/bot.pid` doesn't block a restart. Protective stops stayed at
IBKR the whole time.

---

## Version control and secrets

- `.gitignore` excludes `.env` (and any `.env.*` except `.env.example`), **every `*.csv`**,
  `data/`, `logs/`, `dashboard/`, `STOP`, build output and editor folders.
- `.githooks/pre-commit` refuses a commit that stages a `.env` file, any CSV, anything under
  `data/` `logs/` `dashboard/`, a Telegram bot token, or an IBKR account ID (`DU…` / `U…`
  numbers; all-zero placeholders such as `DU0000000` are allowed in tests). It's activated
  per clone with:
  ```bash
  git config core.hooksPath .githooks
  ```
  To take something back out of the index, use `git rm --cached <file>`; it keeps the file on disk.
- `.gitattributes` stores text files with LF line endings (Windows scripts keep CRLF).

---

## Notes

All timestamps in logs and journals are **US Eastern Time** (`… ET`), and the daily counters
(entries per day, loss halt) roll over at ET midnight, whatever time zone this PC is set to.

Orders are tagged with `orderRef`: Trend Join Long uses `paperbot:entry`, `paperbot:stop`,
`paperbot:exit`; other strategies `paperbot:<key>:entry` etc. The bot only ever manages orders
and positions the running strategy created. Manual trades are left alone.

`scripts/manual/` holds the one-off scripts used while setting up: `ib_paper_check.py` and
`check_connection.py` (read-only connection checks), and `place_nvda_order.py` /
`close_one.py` (manual 1-share MU buy and close on the paper account). They connect directly
and don't use the bot's risk guard.

## Known limitations
- The unit tests use a fake IBKR connection. The bracket orders, OCA linking, stop
  modifications, reconnects and the service/Docker set-ups have **not yet been exercised
  against a real Gateway on every OS**. Watch the first sessions closely.
- Long-only only; no short selling (the risk guard can never open a short).
- Market holidays and half-days are not modelled: on a 13:00 close, a 15:45 entry window
  never opens and a 15:50 exit waits for the next session (the GTC stop protects meanwhile).
- Overnight positions can gap through their stop at the open; the stop then fills at the
  next price, not at the stop price.
- On low-priced ETFs (≈ $40) a stop under ~2.4 % makes per-share commissions exceed 1 % of
  R, so the commission rule skips the entry. Intended, but it can skip quiet sectors.
- If a bracket's entry is partly filled and then cancelled, the stop's quantity is
  corrected on the next loop rather than immediately.
- The universe scan uses historical-data requests. The first scan of a day downloads a year
  of daily bars per symbol and can take several minutes for 100+ symbols. A single slow
  symbol can still take up to about 30 s, because the time budget is checked between symbols.
- The per-order value cap (`max_order_value`: $30,000) is the binding size limit for the
  current watchlist; the 1 % risk and 10 % size limits sit well above it. The approved
  limits are pinned in `tests/test_risk_policy.py`, which also checks them against a
  reference account and watchlist: change a limit there in the same reviewed change.
  Keep orders large enough that the ≈ $2 round-trip commission stays under 1 % of R
  (`max_commission_pct_of_risk`), or the commission-drag rule blocks the entry.
- Limit prices need live quotes (`reqMktData`). A paper account without a market-data
  subscription shared from the live account gets none: entries and partial profits are then
  skipped, and force close prices from 5-minute bars (with an ALERT).
- The prefilter uses free Wikipedia and Yahoo Finance data, which is unofficial and can be
  rate-limited. Ranking 503 symbols by market cap makes about 500 small requests (5 at a
  time), so expect roughly 1–3 minutes. It's a one-time snapshot: stocks that start gapping
  after it runs are not added.
- `logs/safety_log.jsonl` gets one line per symbol evaluated, so a full day of scanning 100
  symbols adds a few MB. `paperbot-rotate` caps it at 5 MB.
