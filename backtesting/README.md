# Backtesting

Historical simulation of every strategy in `config/rules.json`. **It never trades**: it never
imports the order-sending module (`core/engine.py`), its simulated broker raises on any order
method, its IBKR data connection is read-only, and every backtesting process is marked
`PAPERBOT_BACKTEST=1`, which makes `paper-bot`, `paperbot-bot`, `paperbot-cycle` and the live
order functions refuse to run.

> Historical simulation results do not guarantee future performance.

## Quick start

```bash
# from the paperbot folder, venv active (after `pip install --no-deps -e .`)
paperbot-backtest strategies                          # every strategy in rules.json
paperbot-backtest run --strategy trend_join_long      # one strategy, last 120 days, IBKR data
paperbot-backtest run --strategy equity_mean_reversion --provider yfinance   # no Gateway
paperbot-backtest run --strategy macro_swing_etf --start 2024-01-02 --end 2025-12-31
paperbot-backtest all                                 # every strategy (baseline)
paperbot-backtest all --strategy trend_join_long macro_swing_etf
paperbot-backtest propose                             # experiment ideas -> backtesting/experiments/
paperbot-backtest all --with-experiments              # baselines + every experiment + comparisons
paperbot-backtest experiment EXP-trend_join_long-rvol-lower
paperbot-backtest compare <run_id_A> <run_id_B>       # metric-by-metric comparison
paperbot-backtest list                                # past runs
paperbot-backtest report                              # rebuild the dashboard pages and reports
paperbot-backtest report --period weekly              # weekly executive report (or --period monthly)
paperbot-backtest prune --keep 50 --dry-run           # retention: only ever on request
python -m unittest discover -s tests -p "test_backtest_*.py" -v
```

Then open **`dashboard/backtest.html`** (or the **BACKTESTING** item in the dashboard's top
navigation, `paper-bot dashboard --open`). Every run also has a self-contained `report.html`.

## How it reuses the real strategy code

Paperbot runs every strategy through one engine: `strategies/rule_strategy.py` with the
`blocks/` named in `rules.json` and the shared position machinery in `core/orders.py`. The
backtester drives exactly that engine. Each simulated pass calls `rule_strategy.decide(ib, ...)`,
and every request it returns goes through the live `core/risk.RiskGuard`. What is simulated is
only what IBKR provides:

| Simulated | How |
|---|---|
| `ib` object (`SimIB`) | positions, open orders (look like ib_async Trades), historical bars that existed at the simulated time, quotes (bid/ask = last completed bar close ± half-spread) |
| Clock | inside a simulation the strategy modules' `datetime.now()` returns the simulated time (patched only in the backtest process, only while it runs, restored afterwards) |
| State files | the strategy's state, safety log and watchlist go to a temporary folder, never `logs/` |
| Broker | orders filled against the next bars (table below); OCA groups and bracket children as at IBKR |
| Account | net liquidation = cash + positions at the last price; buying power = cash × multiplier |

A new strategy added to `rules.json` is backtestable with no extra code.

| Strategy | Simulated on | Notes |
|---|---|---|
| `trend_join_long` | 5-minute bars | the daily watchlist is rebuilt from daily opens (top gappers ≥ the D3 block) |
| `macro_swing_etf` | 5-minute bars (signals use daily bars + the latest 5-minute close as "live" price) | positions held overnight on GTC stops; signal exits carried across days |
| `equity_mean_reversion` | 1-minute bars | one pass per minute approximates continuous mode |

## Folders

| Path | Tracked | Contents |
|---|---|---|
| `backtesting/config/backtest.json` | yes | every execution, cost, data and validation assumption |
| `backtesting/experiments/EXP-*.json` | yes | rule variants of one strategy: changes, hypothesis, reason (never applied to `rules.json`) |
| `backtesting/results/<strategy>/<run_id>/` | **no** | one folder per run, never overwritten |
| `backtesting/results/<strategy>/_comparisons/` | **no** | baseline-vs-experiment comparisons |
| `backtesting/results/dataset_usage.json` | **no** | runs and rule variants evaluated per dataset (selection bias) |
| `backtesting/reports/<strategy>/` | **no** | `latest.html`, `<run_id>.html`, `<EXP>-latest.html` |
| `backtesting/reports/portfolio/` · `analysis/` | **no** | portfolio summary, weekly/monthly reports, limitations report |
| `backtesting/data_cache/<provider>/<bar>/` | **no** | downloaded bars (one CSV per symbol-day) and `changes.jsonl` (provider corrections) |

Each run folder holds `run_metadata.json` (run id, strategy version = hash of its rules section,
git commit, dataset id, periods, bar size, assumptions, warnings, errors), `config_snapshot.json`
(the strategy's rules section + backtest settings + `config.json` limits; no host, port, account or
secret), `trades.csv`, `orders.csv`, `fills.csv`, `equity_curve.csv`, `daily_performance.csv`,
`monthly_performance.csv`, `metrics.json`, `validation.json` (data quality, checks, the strategy's
own warnings), `trade_context.json`, `events.jsonl`, `execution.log` (JSON lines) and `report.html`.
A failed run keeps its folder with status `failed` and the error; earlier reports are untouched.

## Data

| Provider | Needs | Coverage |
|---|---|---|
| `ibkr` (default) | IB Gateway logged in to the **paper** account | years of 1m/5m bars incl. premarket. Read-only connection, client id `client_id + 3`; live ports and non-`D` accounts refused. Falls back to `yfinance` (recorded in the run) if it cannot connect. Not yet exercised against a real Gateway in tests. |
| `yfinance` | internet | ~30 days of 1m, ~60 days of 5m bars, daily for years. The start date moves forward automatically (RVOL needs earlier sessions). **Too short for conclusions.** |
| `synthetic` | nothing | generated prices for demos and tests only; every report is marked **SYNTHETIC** |

Missing data is reported, never filled in. Data quality (duplicates, unordered or invalid bars,
off-grid timestamps, incomplete sessions, missing symbol-days) is in `validation.json` and on the
*Data quality & validation* page.

## Fill model

| Order | Simulated fill |
|---|---|
| Entry BUY LMT (GTD) | next open ≤ limit → `min(limit, open × (1 + half-spread + slippage))`; else a later bar trading **below** the limit fills at the limit; expires at its GTD time. At most `participation_rate` × bar volume per bar (partial fills). |
| Protective stop (STP, bracket child) | active once the entry filled; open ≤ stop (gap) → open less costs; else low ≤ stop → stop less costs |
| Exits (partial profit, force close, signal exits) | open ≥ limit → open less costs (never below limit); else high > limit → limit; else it keeps working until the strategy cancels/re-prices it, as live |
| Exit sent mid-bar (e.g. 15:51 on 5m bars) | fills in that bar at the lower of its open and close less costs, if not below the limit (flagged estimated) |
| Same bar could hit the stop **and** a working exit | **stop assumed first** and that exit not filled in the bar (conservative), flagged `ambiguous_bar` |
| Commission | IBKR Fixed (`core/risk.round_trip_commission` / 2 per order: $0.005/share, $1 min, 1% max) |

## Validation

* Trading days are split chronologically: 60% in-sample, 20% validation, 20% out-of-sample;
  trades count in the segment where they exit. Three walk-forward folds check consistency.
* Experiments are judged on the **validation** segment (bootstrap range of the difference in
  average trade). Out-of-sample is shown only as an untouched-data check, never to choose.
* `dataset_usage.json` counts every rule variant tried on each dataset; reports warn about
  selection bias when many have been tried.
* Each run re-checks itself: ledger ↔ equity reconciliation, fills ↔ trades, no oversold trade,
  `rules.json` unchanged (SHA-256), process isolated. A 3× spread/slippage stress run is included.

## Experiments

`paperbot-backtest propose` writes `EXP-<strategy>-<idea>.json` files from each strategy's blocks
(e.g. lower RVOL threshold, larger gap, wider stop, later breakeven, deeper RSI pullback, wider
VWAP stretch, longer time stop, later first entry) and the latest baseline's results. A file:

```json
{"id": "EXP-trend_join_long-rvol-lower", "strategy_id": "trend_join_long",
 "changes": {"entry.7.min": 1.5}, "baseline_values": {"entry.7.min": 2.0},
 "hypothesis": "...", "reason": "...", "evidence": "...", "status": "proposed"}
```

`changes` are paths inside `rules.json → strategies.<key>` (list positions as numbers). A variant is
validated by the bot's own profile loader before it runs. **Nothing is ever applied to
`config/rules.json`**: a variant that holds up on validation and out-of-sample data still needs a
person to review it and edit `rules.json` by hand.

## Portfolio

Each strategy is simulated on its own capital. With one completed baseline the portfolio page shows
that full simulation; with several it shows them side by side, and the combined figures (summed
daily P&L, concentration, days on which several strategies lost) are labelled **indicative**:
shared cash and account-wide limits across strategies are not simulated together.

## Scheduling

Manual only (`schedule.enabled: false` in `backtest.json`): no scheduled job is registered. If you
add one later, run it outside US market hours. `paperbot-backtest` uses its own lock
(`results/.backtest.lock`), never the bot's client id, state or pid file, and only reads from IBKR.

## Known limitations

See the dashboard's *Limitations & analysis* page: survivorship bias (today's index members), no
historical quotes, bar resolution (ambiguous bars), pass timing approximates continuous mode,
exits not volume-capped, cash-only buying power by default, the drawdown breaker stays tripped (as
live) unless `reset_drawdown_breaker_next_day` is set, and the portfolio is indicative across
strategies.
