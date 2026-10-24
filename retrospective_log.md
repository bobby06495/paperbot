# Paperbot Retrospective Log

**Strategy:** Trend Join Long (Paper Bot Strategy) · **Broker:** Interactive Brokers, paper account
**Purpose:** A dated record of structural flaws found in the trading bot, the fixes applied and the lessons kept. Entries are listed in calendar order, oldest first.

---

## October 20, 2026: Execution, timing and risk hardening

**Scope:** 17 issues fixed: 13 execution, timing and risk flaws, and 4 structural ones (the strategy hard-wired into the engine, a Windows-only runtime that could trade paper only, no exits other than the daily force close, and rules that made scalping impossible). The test suite grew from 187 to 400 tests, all passing, with no regressions.

**Summary for the reader in a hurry.**

| # | Flaw | Exposure | Fix |
|---|---|---|---|
| 1 | Force close started at 15:55, not 15:51 | Fewer exit attempts before the close | 15:50 run stays connected and starts at 15:51:00 |
| 2 | 15:30 run never scanned | Lost last entry slot | Entry window compared to the minute |
| 3 | 16:00 race left positions without a stop | Naked overnight position | Poll until IBKR confirms DAY orders dead |
| 4 | Stop cancelled before the closing SELL | Up to 60 s unprotected per attempt | OCA group links SELL and stop at IBKR |
| 5 | Limits priced from 5-minute bar closes | Stale prices, missed or bad fills | Live `reqMktData` top of book |
| 6 | Unfilled entries lived until 16:00 | Late, unintended fills | GTD expiry at 15:31 plus sweep |
| 7 | Partial profit was one-shot | Profit-taking forfeited | Pending state, retry up to 3 times |
| 8 | No commission-drag check | Fees eat the edge on tiny trades | Block when fees exceed 1 % of R |
| 9 | Buying power never queried | Margin rejects or over-commitment | Cap and reject on IBKR BuyingPower |
| 10 | No multi-day loss limit | Unbounded capital bleed across days | Latched drawdown breaker, $3,000 |
| 11 | Thresholds split over two files, parsed twice, unchecked | Silent overrides, mid-session config failures | One home per key, one typed parser, fail closed |
| 12 | Risk limits raised with no portfolio-level regression check | Unreviewed drift of what the bot may do | Approved-limits lock plus reference-account scenario tests |
| 13 | Strategy hard-wired into the engine | Every new strategy = engine surgery, regression risk | Generic engine + rules.json strategies built from blocks, start-up menu |
| 14 | Windows-only runtime, paper-only by code, no reconnect | Not portable; live needs code edits; Gateway restart = crash | `paper-bot` CLI, built-in scheduler, auto-reconnect, paper/live by config |
| 15 | Engine could only flatten every day at a fixed time | No swing strategies; exits only by stop or force close | Signal-exit framework, 1d timeframe, multi-day holding; Macro Swing ETF |
| 16 | One global fee rule and one entry per symbol per day | Scalping strategies could never trade | Per-strategy fee limit and re-entry count; Equities Mean Reversion |
| 17 | Start-up could not show or cross-check what was about to trade | A stale choice, edited rules or the wrong Python could reach IBKR silently | Pre-connect PREFLIGHT summary and checks, scan state per strategy, one verified interpreter |

---

### 1. October 20, 2026: End-of-day force close started 4 minutes late in scheduled mode

**Identified Flaw / Risk.**
`rules.json` sets `force_close_et` to 15:51, and the continuous bot honoured it. The deployed scheduled mode runs every 5 minutes, though. The 15:50 run finished before 15:51 and the next run only started at 15:55. Liquidation therefore began 4 minutes late, which cut the re-pricing attempts from about 9 to about 5. In a thin or falling closing auction, fewer attempts make it more likely the position is still open at 16:00 and carried overnight.

**Changes Implemented.**
- `cycle.seconds_to_force_close()` detects the last scheduled run before force close (the next run would start after 15:51).
- If that run holds bot positions, it stays connected, sleeps until **15:51:00** with `sleep_until()` (events keep flowing, and the STOP file is honoured every 5 s), then enters the existing 30-second force-close loop.
- The pid lock makes the 15:55 run skip, so two processes never manage the same orders.

**Engineering Reasoning.**
Adding a 15:51 scheduled task would have been a second, separate path to the same time, and it would also have had to be kept right across DST changes. Extending the run that already holds the connection, the pid lock and the account state keeps one owner of the position, with no reconnect cost at the most time-critical moment.

**Key Learning.**
A configured time is only a guarantee if the scheduler can actually wake up at it. Every time-based rule should be tested against the real trigger grid (5-minute runs, process start-up delay), not just the config value.

---

### 2. October 20, 2026: The 15:30 entry scan never ran

**Identified Flaw / Risk.**
The entry window check was already written as `<= 15:30`, but it compared to the second. The 15:30 run starts a few seconds after 15:30:00 because of process start-up, so `15:30:04 <= 15:30:00` was false and the run skipped its scan. The last effective scan was 15:25, so the configured window was silently 5 minutes shorter than specified.

**Changes Implemented.**
- `strategy._in_entry_window()` compares at **minute precision**, so the whole 15:30 minute is inside the window. Both `decide()` and the read-only `check_symbol()` use it.
- The dashboard's phase model (`telemetry.phase`) uses the same rule, so the UI and the strategy agree.
- The scan stops if the clock passes the entry cutoff during a long (up to 150 s) scheduled scan.

**Engineering Reasoning.**
The config is written in `HH:MM`, so the natural reading is "through the end of that minute". Matching the comparison to the config's precision fixes the boundary without padding the time by an arbitrary number of seconds.

**Key Learning.**
Compare times at the precision they are configured in. An inclusive `<=` against a value to the second is effectively exclusive for any process that cannot start at exactly :00.

---

### 3. October 20, 2026: The 16:00 post-close race could leave a position without a stop overnight

**Identified Flaw / Risk.**
When the force close does not fill, the 16:00 run restores a GTC stop for the overnight position. The stop is sized as `shares held − shares on working exits`. If IBKR had not yet processed the expiry of the DAY force-close SELL when the run read the order book, that SELL still counted as covering every share. The stop target became 0 and **no stop was placed**. The 16:05 slot was gated closed, so the position could stay **unprotected until the next morning**, with no broker-side stop through the after-hours and pre-market sessions.

**Changes Implemented.**
- New `bot.await_day_orders_done()`. It polls `reqAllOpenOrders()` and the order-status stream until every `paperbot:entry` and `paperbot:exit` order is confirmed **Cancelled or Filled** by IBKR (up to 90 s). If any is still alive, it sends an explicit cancel and waits up to 5 s more.
- If confirmation never arrives, or the connection drops, it logs an **ALERT** and returns False. The reconcile then places no stop beside the possibly-live order, because a stop next to a live exit could double-sell. `decide()` also ALERTs when it sees a live DAY order after the close.
- `cycle.py` has a new `post_close` gate (16:00–16:10). The check runs **before** the pass, and the 16:05 run is now a second protect-only pass.
- The force-close run, which holds the pid lock through 16:00, does this check itself if it is still holding at the close, because the 16:00 run would be skipped.
- Continuous mode (`bot.py`) runs the same check once per trading day after the close.

**Engineering Reasoning.**
The exposure calculation is only as correct as the order snapshot it reads. Instead of guessing ("ignore DAY orders after 16:00"), the bot waits for the broker's own confirmation, which is the authoritative state. If that confirmation never arrives, the failure mode is still to avoid a double-sell, but now the operator gets an ALERT and there is a second pass at 16:05.

**Key Learning.**
Never derive a safety-critical quantity from a snapshot taken during a state transition, such as session close or order expiry. Wait for the broker to confirm the state you depend on, and keep a retry path after it.

---

### 4. October 20, 2026: The stop was cancelled before the closing order was placed

**Identified Flaw / Risk.**
Force close cancelled the protective stop, waited for the cancel to be confirmed, and only then sent the closing SELL limit. For up to 60 s per re-pricing attempt, which could add up to several minutes across attempts, the position had **no stop at all**. A sharp drop in the last minutes of the session during that window would have been taken in full.

**Changes Implemented.**
- Every new protective stop (bracket child and restored stops) gets a unique **OCA group** (`ocaType = 2`, reduce remaining orders, with overfill block). The name is a fresh UUID each time, because IBKR cancels orders sent into a group that has already filled.
- Force close **no longer cancels the stop**. It sends the closing SELL in the stop's OCA group, which is read from the live order IBKR reports. When the SELL fills, IBKR cancels or shrinks the stop. If the stop triggers first, IBKR shrinks the SELL.
- A stale SELL is still cancelled and replaced at a wider price, but the stop stays live the whole time.
- A stop placed before this change has no OCA group, so it keeps the previous confirmed-cancel path. Nothing is left unhandled.

**Engineering Reasoning.**
Mutual exclusion between the stop and the exit used to be enforced by asynchronous local code (cancel, poll, then send), which needs a gap to be safe. IBKR's OCA mechanism enforces it **on the broker's servers, atomically**, so both orders can rest at the same time without any risk of double-selling. The safety guarantee no longer depends on the bot process, the network or the timing of a cancel confirmation.

**Key Learning.**
Prefer broker-side atomic order relationships (brackets, OCA, attached orders) over cancel-then-replace sequences in local code. Any "cancel A, then place B" flow on a protective order is an unprotected gap by design.

---

### 5. October 20, 2026: Limit prices came from 5-minute historical bar closes

**Identified Flaw / Risk.**
Entry, partial-profit and force-close limit prices were computed from the close of the last completed 5-minute bar, which can be up to about 5 minutes old. In a fast market, a BUY limit could sit below the real ask and miss the fill, or a SELL limit could sit far from the bid. No check existed for halted or crossed markets.

**Changes Implemented.**
- New `strategy._quote()`. It keeps a **streaming `reqMktData` subscription** per symbol and waits up to 3 s for the first tick. It accepts a quote only if bid > 0, ask > 0, bid ≤ ask, and the halted flag is not set.
- BUY limits are priced from the **ask**, SELL limits from the **bid**, with the existing 0.2 % marketable offset (widening on force-close attempts).
- No valid quote: entries and partial profits are skipped (fail closed, retried next pass). Force close falls back to the last bar close with an **ALERT**, because getting flat takes priority over price.
- Subscriptions no longer needed are cancelled every pass, staying within IBKR's market-data line limits.
- The signal filters and the R triggers still use completed 5-minute bars, so the strategy's rules are unchanged.

**Engineering Reasoning.**
Signals and execution have different jobs. The bar-based filters define the strategy, while limit prices are an execution concern that needs the current top of book. Pricing from the far touch makes the marketable limit actually marketable.

**Key Learning.**
Separate signal data from execution data. Historical bars are fine for deciding *whether* to trade; *at what price* needs a live quote, plus a defined fallback for when that quote is missing.

---

### 6. October 20, 2026: Unfilled entry orders lived until the 16:00 expiry

**Identified Flaw / Risk.**
A BUY limit that did not fill stayed live until IBKR's DAY expiry at 16:00. It could fill late in the afternoon (for example at 15:45), opening a position minutes before the force close. Meanwhile it held one of the two `max_open_orders` slots and a concurrency slot all afternoon.

**Changes Implemented.**
- Entries are sent as **GTD**, expiring at **15:31:00 ET**, the end of the inclusive 15:30 minute. IBKR expires the bracket parent and cancels its child stop, even if the bot is down.
- A local **sweep** in `decide()` cancels any `paperbot:entry` still working from 15:31 on, in case IBKR has not expired it yet.
- The risk guard rejects a BUY whose GTD time has already passed.

**Engineering Reasoning.**
The cutoff is 15:31 rather than exactly 15:30:00 because, with the inclusive 15:30 scan (fix 2), a 15:30:00 cutoff would cancel the orders that scan had just placed. GTD puts the deadline on the broker side, and the local sweep and guard add two more layers behind it.

**Key Learning.**
Every order should have an explicit lifetime that matches the strategy's intent. If an order's lifetime depends on default expiry, it lives longer than intended.

---

### 7. October 20, 2026: Partial profit-taking was a one-shot attempt

**Identified Flaw / Risk.**
`partial_done` was set as soon as the partial-profit SELL was *sent*. If that limit did not fill within 60 s (it was then cancelled) or expired, the partial profit was **forfeited for the rest of the trade**, even if the price stayed above the target.

**Changes Implemented.**
- A small state machine: **idle → pending → done**, or back to idle. `on_sent()` records `partial_pending` (the share count it was sent against) and `partial_attempts`.
- Once no partial order is working: fewer shares held means it filled, so the state becomes done. Unchanged shares means it expired or was cancelled unfilled, so the state resets.
- After a reset, the next pass's reconcile restores the full-size stop, and the following pass **recalculates the target from fresh data and retries**. Retries are capped at **3 attempts** per position, so the stop is not shrunk and restored every minute.

**Engineering Reasoning.**
State is now set from the **outcome** observed at the broker (the position size), not from the local act of sending. This works however the order ended: a fill, the 60-s cancel, a DAY expiry or a manual cancel.

**Key Learning.**
Set state on outcomes confirmed by the broker, not on actions taken. "Order sent" is not "profit taken".

---

### 8. October 20, 2026: No guard against commission drag

**Identified Flaw / Risk.**
Nothing compared the cost of a trade with its risk. Under the live caps (1 share), a trade risking about $6 paid about $2 in round-trip commission, which is **33 % of R**. Each trade needed a large move just to break even, a structural capital bleed that per-trade risk limits did not catch.

**Changes Implemented.**
- `risk.round_trip_commission()` models IBKR Fixed pricing: max($1.00, $0.005/share) per side, capped at 1 % of trade value.
- `risk.commission_drag()` blocks an entry when the estimated round-trip commission exceeds **`max_commission_pct_of_risk` (1 %) of R** (R = qty × (limit − stop)).
- It is enforced in sizing (skipped quietly) and in the risk guard (BLOCKED with a reason). The threshold is configurable in `rules.json`, and `null` disables it.

**Engineering Reasoning.**
The check sits in the risk guard, the single point every order passes through, so no code path can skip it. The sizing pre-check keeps the log free of a BLOCKED warning for every 5-minute bar.

**Operational note.** With `max_quantity_per_order = 1`, **every entry is now blocked**. This is intended: the rule correctly shows that 1-share trades cannot pay for their commission. Entries resume once the caps allow positions with R of about $200 or more.

**Key Learning.**
Transaction costs are a risk input, not an accounting afterthought. Position-size floors should come from cost-to-risk ratios, not only from caps.

---

### 9. October 20, 2026: Buying power was never checked before sending orders

**Identified Flaw / Risk.**
Sizing used net liquidation and fixed caps but never asked IBKR how much **buying power** was available. With other positions or margin use in the account, an order could be rejected by the broker or commit capital the account did not have.

**Changes Implemented.**
- `account.buying_power_usd()` reads IBKR's `BuyingPower` from the account-value stream (`updateAccountValue`). For this non-USD account it converts to USD with the same FX path as net liquidation.
- Sizing **caps** quantity at floor(BuyingPower ÷ limit), and buying power used by earlier signals in the same scan is subtracted.
- The risk guard **rejects** any BUY whose value exceeds buying power, and **blocks** entries when buying power is unknown (fail closed).
- Exits and stops are never blocked by this check.

**Engineering Reasoning.**
The broker's own margin figure is the authority. Reusing the existing currency-conversion path avoids a second, possibly inconsistent USD figure. Failing closed matches how net liquidation was already handled.

**Key Learning.**
Size against the account constraint that actually binds, which is what the broker will accept, and fail closed when that figure is unavailable.

---

### 10. October 20, 2026: No multi-day cumulative drawdown limit

**Identified Flaw / Risk.**
The only loss control was the daily halt at −$1,000, which **resets every morning**. A run of losing days, each just under the daily limit, could keep draining capital with nothing to stop it.

**Changes Implemented.**
- `risk.DrawdownBreaker` keeps a persistent **`logs/drawdown_state.json`**. Every pass stores today's IBKR daily P&L under the ET date, keeping the last value of each day. The cumulative sum is the equity curve, and its peak is tracked.
- When the cumulative P&L falls **$3,000 (`max_drawdown_usd`) below its peak**, the breaker trips. New entries are blocked and working entries and exits are cancelled. Stops and exits stay allowed. It **stays tripped across days and restarts**.
- It is re-armed only by hand: `paperbot-bot --reset-drawdown`, which refuses to run while the bot is running so the bot cannot overwrite the reset. The current level becomes the new peak.
- The file is written atomically and read **fail closed** (a corrupt file stops the bot). It is also covered by `--reset-state` and `check_setup`.

**Engineering Reasoning.**
The curve is built from daily P&L, not net liquidation, because the account's base currency is not USD. SGD/USD moves and deposits would otherwise trip or hide a drawdown that has nothing to do with the strategy. Latching until a person re-arms it makes a sustained losing streak a decision for a person, not something the next session clears automatically.

**Key Learning.**
Daily limits bound a single bad day, not a bad week. Persistent, latched limits are needed for slow, cumulative failure modes, measured in a unit that only the strategy can move.

---

### 11. October 20, 2026: Risk thresholds split across two files, parsed twice, never type-checked

**Identified Flaw / Risk.**
Quantitative thresholds sat in two places: per-order caps, entry counts and the daily loss halt in `config.json → limits`, everything else in `rules.json → risk`. They were merged with "rules.json wins on overlap", so a key in both files was overridden silently. The strategy's sizing read `config.json` with its **own** parser, separate from the risk guard's, so the two could disagree. No value was type-checked: a string such as `"1"` loaded without complaint and only failed at the first order check, mid-session.

**Changes Implemented.**
- The 5 thresholds `max_quantity_per_order`, `max_order_value`, `max_orders_per_day`, `max_open_orders` and `max_daily_loss` moved to `rules.json → risk`. `config.json → limits` keeps only the safety rails `allowed_symbols` and `limit_orders_only`. No value changed.
- `risk.load_limits()` is the **single parser**. Each key has one home and an explicit type (`int`, `float`, `bool`, list of symbols), and every threshold must be > 0. Booleans and numeric strings are rejected as numbers, and unknown keys are rejected.
- **Fail closed:** a missing key, an invalid value, or a threshold left in `config.json` (for example a restored backup) stops the bot before it connects, with an ALERT naming the key (`risk.require_limits`). There are still no default values for any cap.
- Strategy sizing (`_caps`) is now filled from that same parser. The dashboard shows the merged limits, and the "Source" column says `rules.json` for the moved keys.

**Engineering Reasoning.**
With one source per value and one parser, the risk guard and the sizing can never disagree. Refusing a duplicate key, instead of picking a winner, removes a silent override path for a safety cap. Validating at startup moves configuration errors from the middle of the session to before the first connection.

**Key Learning.**
Every configuration value should have exactly one home and one parser, validated at startup. "Last file wins" merging hides mistakes in exactly the values that matter most.

---

### 12. October 20, 2026: Risk limits raised 12× without a portfolio-level regression check

**Identified Flaw / Risk.**
The order caps were raised to make high-priced watchlist names tradable: shares per order 1 → 500, order value $2,500 → $30,000, daily loss $1,000 → $2,000, drawdown $3,000 → $6,000. The new values had only been checked in an analysis done in chat. No test tied them to the account, and the live-config test checked only types. A later edit could therefore move the risk limits anywhere, for example a $300,000 order cap, and every test would still pass.

**Changes Implemented.**
- New `tests/test_risk_policy.py`, which runs against the **live** `config.json` and `rules.json`:
  - **Approved-limits lock:** `APPROVED_LIMITS` pins every limit exactly, with the approval date and reason. Any change to the config fails the test until the table is updated in the same reviewed change.
  - **Reference scenario:** a $781,120 reference account (the paper account on 2026-10-19) and the 13 watchlist symbols at their 2026-10-19 opens ($94.53–$1,358.50). Each is tested with a typical 2% stop and the tightest possible stop of about 1.2%, and sized by the bot's real `_size_entry`. Every trade must be tradable, stay within the share, order-value, 10%-size and 1%-risk limits, keep commission ≤ 1% of R, and be approved by the `RiskGuard`.
  - **Limits checked against the account:** one order ≤ the 10% position cap; all concurrent positions ≤ 25% of the account; daily loss halt ≤ 0.5% of the account and ≥ the worst single stop-out; drawdown breaker ≤ 1% of the account and ≥ the daily loss halt.
- **Checked by deliberately breaking the config.** A 4× larger order cap, a return to $2,500, and a $10,000 daily loss each fail the lock and the matching limit check. The $2,500 case reports all 26 reference trades as untradable, each by name.
- The spec (§1.4, §2.4, §3.2, §6) and the README now show the new limits and where they are pinned.

**Engineering Reasoning.**
Limits are configuration, but they define what the bot is *allowed* to do, so they need the same change control as code. Pinning exact values alone would only record that a number changed. Scenario checks alone would allow drift within their bounds. Together, every change shows up in a reviewed diff and is checked against the portfolio model before it can merge.

**Key Learning.**
Treat risk limits as policy: pin the approved values, test them against a realistic reference portfolio, and confirm the test fails when the config is deliberately broken. A test that cannot fail is not a safety net.

---

### 13. October 20, 2026: The strategy was hard-wired into the engine

**Identified Flaw / Risk.**
Trend Join Long's filters, exits and timings lived in one module that the runner imported directly. The runner made 12 calls into it, the scheduled pass made 11, and the scheduled pass even wrote to its internals. The order machinery (stop sync, orphan stops, force close, partial profit) was mixed in with the strategy's own rules. `rules.json` held a single strategy at its top level. Adding or switching a strategy meant editing the engine, and every edit risked changing how existing positions are protected.

**Changes Implemented.**
- **Separated the runner from the strategy.** `core/` is the runner (engine, risk guard, sizing, order machinery, market data) with no strategy knowledge. `strategies/rule_strategy.py` is one generic strategy that runs any profile.
- **Strategies are `rules.json` sections built from named blocks.** `blocks/` holds the entry checks, stop rules, management and exits; `indicators/` holds pure functions. `core/profile.py` validates a whole section at start-up, and every mistake names its exact place, e.g. `strategies.x.entry[1]: unknown entry type ...`.
- **Start-up selector** (`core/selector.py`):
  - a menu built from `rules.json`, plus `--strategy` and the saved choice;
  - an invalid section is listed but cannot be chosen;
  - switching is refused while the previous strategy still manages positions.
  All of this happens before any broker connection.
- **Each strategy keeps its orders and state separate.** It gets its own order tags (`paperbot:<key>:…`) and state folder. Trend Join Long keeps the original tags and paths, so its working IBKR stops carried over.
- **One shared sizing module** (`core/sizing.py`): whole shares, the same limit order for every strategy, and prices rounded to the tick in the safe direction (BUY limits up, SELL limits and long stops down).

**Engineering Reasoning.**
A behaviour baseline was recorded from the old code *before* anything changed: 31 scenarios covering every filter's pass and fail path, sizing, and the order and state outcomes. The refactored engine reproduces all of them exactly. The only intended change came afterwards, as a separate step: directional rounding moved 13 prices by exactly $0.01 in the safe direction, and no quantity or filter result changed. With a generic engine plus declarative blocks, a new strategy is a `rules.json` section. A new kind of signal is one small block class, with no change to the engine, risk or sizing code.

**Key Learning.**
Before restructuring trading code, freeze its behaviour in a recorded baseline and prove the new code reproduces it. Make any deliberate behaviour change a separate, visible step.

---

### 14. October 20, 2026: A Windows-only runtime that could trade paper only, and crashed when IB Gateway restarted

**Identified Flaw / Risk.**
Running the bot depended on Windows: Task Scheduler (13 `HT_*` tasks re-registered whenever daylight saving changed), PowerShell launchers, power-setting changes, a Windows-only Python path and a Win32 process check. It could not run on macOS, Linux or a server. "Paper only" was enforced by hard-coded ports and the account prefix, so going live meant editing code. The long-running bot had no reconnect: IB Gateway's daily restart or a network drop ended it.

**Changes Implemented.**
- **One command on every platform.** `paper-bot start` (`cli.py`) runs the whole trading day in one process: strategy passes plus a **built-in scheduler** (`core/clock.py`) for log rotation, the watchlist and the daily summary. It runs on the US Eastern clock, with no re-registration at daylight saving. A missed slot within its grace period catches up, and every run is recorded for the dashboard.
- **Automatic reconnect.** When IB Gateway is unreachable, the bot sends one ALERT per outage and retries with back-off (30 s up to 5 min). After reconnecting it checks the account again; a different account stops the bot. Protective stops stay at IBKR throughout.
- **OS-specific code isolated in `system/`.** It holds the process check, keep-awake (without changing power settings), and `paper-bot service install` for systemd (Linux), launchd (macOS) and Task Scheduler at log-on (Windows). `deploy/` adds Docker with a headless IB Gateway for cloud servers.
- **Paper or live by configuration only** (`core/broker.py`): `config.json` `mode` plus `connections`, with no code change.
  - **Ports and accounts must match the mode.** Paper and live ports never overlap. Live accepts only the exact configured account; paper accepts only one `D…` account.
  - **Live start-up must be confirmed:** by typing the account id at the terminal, or with `PAPERBOT_LIVE_CONFIRM` for unattended runs.
  - **Live keeps its own data.** It has separate state, logs, ledger and dashboard folders, and every push is prefixed `[LIVE]`.

**Engineering Reasoning.**
Scheduling inside the bot removes the OS dependency and the daylight-saving re-registration in one move, and keeps a single owner of the connection and the pid lock. Live trading is config-only but never one setting away from real money: the mode, the ports, the exact account and an explicit confirmation must all agree. Every mismatch refuses to start.

**Key Learning.**
Keep platform specifics behind one small boundary and drive environment differences (paper vs live, host vs container) from configuration with strict cross-checks. Long-running trading processes must expect their broker connection to drop, and recover without losing protection.

---

### 15. October 20, 2026: Positions could only leave by their stop or the daily force close

**Identified Flaw / Risk.**
Every strategy had to be flat at a fixed time each day, and a position could only exit through its protective stop or that force close. Signals came only from completed intraday bars. A multi-day strategy (hold overnight, exit on a signal or after N days) could not be expressed. Several components also quietly assumed a 15:51 force close existed: the scheduled pass, the dashboard phases and the timeline.

**Changes Implemented.**
- **Signal-exit framework** (`core/orders.signal_exits`). Exit blocks decide *when* to exit; the closing itself reuses the force close's gap-free path: a marketable SELL in the stop's OCA group, re-priced until filled, with the stop live throughout. A decided exit is recorded in the position state and carried through re-pricing and into the next session. Multi-day strategies never exit on the entry day.
- **Daily timeframe and multi-day holding.**
  - **Data:** `1d` strategies see the completed daily bars plus today's live mid price as the close.
  - **Holding:** `multi_day` keeps positions overnight on their GTC stops, with no force close and no "still open after the close" alert.
  - **Scan cadence:** at most every 5 minutes inside the entry window.
- **Validation.** Start-up refuses:
  - a multi-day strategy with a force close;
  - a `1d` strategy using an intraday-only block, naming the block;
  - a strategy without exits;
  - a `time_stop` with both or neither of `days` and `minutes`.
- **New indicators:** Wilder RSI and ATR.
- **New blocks:** `close_above_sma` and `rsi_below` (entry), `atr_multiple` (stop), `close_above_sma` and `time_stop` (exit). The trading-day count uses the daily bars, so weekends and holidays are not counted.
- **No force-close time:** the scheduled pass, telemetry and the dashboard handle it (the phase becomes zero-length). The strategy page has a generic "Strategy blocks" panel.
- **Macro Swing ETF** added purely as a `rules.json` section, with the approved draft settings.
- **Per-strategy limits lock:** `test_risk_policy.py` now requires every strategy in `rules.json` to have approved limits and a reference scenario.

**Engineering Reasoning.**
Separating *when* to exit (a block) from *how* to exit (one shared, already-tested closing path) gives every future exit rule the OCA protection for free. Trend Join Long's force close was moved onto that shared path without a single change to its recorded baseline. The reference scenario for the new strategy surfaced a real interaction: per-share fees on a ~$40 ETF exceed 1% of R when the stop is under ~2.4%. The commission rule then skips the trade, which is intended and is now documented.

**Key Learning.**
Model the lifecycle explicitly (entry, management, exit, holding period) rather than assuming "flat by close". Every hidden default (here the 15:51 force close) is a latent bug for the next strategy that doesn't share it.

---

### 16. October 20, 2026: Rules that made a scalping strategy impossible

**Identified Flaw / Risk.**
Two engine-wide rules made an intraday index-ETF scalper impossible:
- **The fee limit.** At ~$600 a share, a $30k order cap and ~0.15% one-sigma stops, round-trip fees are about 2–5% of R, far above the global 1% limit, so every trade was blocked.
- **One entry per symbol per day.** A mean-reversion strategy that exits at VWAP could not take the next stretch.

**Changes Implemented.**
- **Fee limit per strategy.** It comes from each strategy's own risk block. Only Equities Mean Reversion allows 5%, as approved; the other strategies keep 1%.
- **Re-entries per strategy.** A new optional `max_entries_per_symbol_per_day` (default 1) is counted per ET day in the strategy state. An older state counts as one entry, so Trend Join Long's rule is unchanged. The global 5-entries-per-day cap still applies.
- **New indicator and blocks.** The session VWAP and its volume-weighted standard deviation; blocks `below_vwap_sigma` (entry), `sigma_below_entry` (stop) and `target_vwap` (exit). The 30-minute exit reuses `time_stop`.
- **Equities Mean Reversion as a `rules.json` section:** SPY, QQQ and IWM on 1-minute bars, long only, flat by 15:50, continuous mode only. Scheduled 5-minute runs are refused before anything connects.
- **Reference scenario.** Its limits are locked in `test_risk_policy.py`, with quiet (0.15%) and busy (0.3%) one-sigma stops.

**Engineering Reasoning.**
Making a safety threshold per strategy, rather than relaxing it globally, keeps the stricter 1% fee rule for the strategies it suits. It makes the cost of scalping explicit and reviewed: it sits in the approved-limits table with its reason. The default for re-entries is the old rule, so existing strategies cannot change behaviour by omission; the Trend Join Long baseline proves it.

**Key Learning.**
Risk rules that encode a trading style (fees vs. R, entries per day) belong to the strategy, not the engine. Defaults must reproduce the old behaviour exactly.

---

### 17. October 20, 2026: Start-up that could not prove what it was about to trade

**Identified Flaw / Risk.**
A code review of the multi-strategy refactor found that nothing visibly cross-checked the selected strategy, its limits and the runtime before a connection was made:
- **No start-up summary for unattended runs.** The effective limits were logged only after connecting.
- **No cross-check.** A stale saved choice, a `rules.json` edited after the menu, or a risk guard built from other limits would not have been detected.
- **Stale scan state.** The entry-scan position was module-global and not tied to a strategy.
- **Crashing blocks.** An unexpected error in one entry or exit block (a bug, odd data) aborted the whole pass instead of failing one symbol.
- **Ambiguous launcher.** `start_paperbot.ps1` silently took the first of two `.venv` folders.
- **Copied old-path modules.** The old import paths (`paperbot.trading.*`, `runtime.cycle`) were `import *` copies, so settings changed through one path did not reach the other.
- **Dashboard in the loop.** The pandas dashboard rebuild ran inline, after every pass.

**Changes Implemented.**
- **`engine.preflight()`**, called by `paper-bot start` and the scheduled pass after every other check and before any connection:
  - It logs `PREFLIGHT` lines: the connection, the strategy and label, the `rules.json` SHA-256 (first 12 hex digits), timeframe, window, universe, blocks, and every effective limit.
  - It refuses to start with an ALERT if the selected strategy (saved choice, else default) differs from the one loaded, if the strategy module runs another profile, if the risk guard's limits differ, or if `rules.json` no longer matches what was loaded.
- **Scan state per strategy.** It now records its strategy key and restarts when another strategy runs. `configure()` clears it. `decide()` refuses (ALERT) to run a profile other than the selected one.
- **Block errors.** One `RESULT_DEFAULTS` (in `blocks/context.py`) fills every result key before any block runs. An unexpected error in an entry or stop block becomes `FAIL <block>: error ...` for that symbol only, and stays in the safety log. An exit check that crashes skips only that position, whose stop stays at IBKR.
- **Launcher.** The interpreter is `-Python`, else `PAPERBOT_PYTHON`, else the single `.venv` found. With two `.venv` folders the script stops and lists both. The chosen interpreter is printed and must import `paperbot` from this project's `src`.
- **Old import paths are aliases** of the new modules, the same objects, so the two can never diverge.
- **Background dashboard.** The dashboard rebuild, which uses files only, runs in one background thread and is skipped while the previous one runs. Heartbeat and telemetry stay inline, since the ib cache is not thread-safe, and are timed: a warning above 2 s. Processes wait for the last rebuild before exiting.
- **Tests (42 new):**
  - preflight refusals and summary;
  - exact-second ET boundaries, Friday to Saturday, the PC's zone vs. ET, and the end of daylight saving (2026-11-01);
  - unconfirmed post-close orders in both run modes, and a force close that ends once flat;
  - block crashes and scan reset;
  - account-value parsing (unusable values, segment and BASE rows, zero and negative values);
  - the harness running the real Trend Join Long profile;
  - the old-path aliases;
  - the non-blocking dashboard.

**No change needed** for three review points: the limits are already locked to the exact approved values (`APPROVED_LIMITS`, entry 12) and to reference scenarios. `test_risk.py` now points there.

**Engineering Reasoning.**
Each check runs before a connection and turns a silent mismatch into a refusal with a reason, which is consistent with the fail-closed approach of entries 11–12. The summary goes to the log on every start, attended or not, so the record of what traded is in the log, not only on a terminal. Isolating a block failure to one symbol keeps protective work going: exits, stops and other symbols. Strict start-up checks and resilient per-symbol evaluation work together: refuse to start when the configuration is wrong, keep running when a single input is wrong.

**Key Learning.**
A safety-critical process should state, before it acts, exactly what it is going to run, and check that statement against every place that state is kept. Compatibility shims should be aliases, never copies.

---

### Verification and follow-up for October 20, 2026

- **Tests:** 400 pass (187 before). New coverage includes OCA force close, the post-close order confirmation (expiry, forced cancel, unconfirmed cancel, connection drop, dry run), the 15:50 to 15:51 handover, the minute-precision window, GTD and the sweep, the partial-profit retry states, quote validation and release, buying-power capping, commission drag and the drawdown breaker (persistence, latching, reset, corrupt file) and the limits loader (each key from its file, type casting, missing, invalid, unknown and misplaced keys).
- **Error handling:** connection drops and API timeouts in the new paths either fail closed (entries skipped, no stop beside an unconfirmed order) or degrade with an ALERT (force close prices from bars). Exits and protective stops are never blocked by any new risk check.
- **Open item:** OCA linking, GTD orders, `reqMktData`, reconnects and the service/Docker set-ups are verified only against fake connections and dry runs. The first `--submit` paper session needs close watching: confirm that the bracket child keeps its `ocaGroup`, that force-close SELLs join it, and that the paper account receives live quotes.
