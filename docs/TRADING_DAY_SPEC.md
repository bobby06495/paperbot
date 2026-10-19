# Paperbot — Trading-Day Technical Specification

**Strategy:** Trend Join Long (long only, 5-minute bars) · **Broker:** Interactive Brokers, paper account only (port 4002) · **Code basis:** `main` @ `addfa52`

This document traces what Paperbot does on one US trading day, from the 09:30 ET open to the 16:00 ET close. Every rule, threshold and timing below comes from the code (`src/paperbot/**`) and the live config (`config/config.json`, `config/rules.json`). Where the code behaves differently from what the config suggests, the difference is stated in the section where it applies and listed again in §6.

All times are **US Eastern (ET)**. The PC runs on Singapore time, and the scheduler converts every ET time to local time.

---

## 0. Two run modes (read this first)

Paperbot has two ways to run. Both use the same decision code: `strategy.decide()` → `RiskGuard` → `bot.handle_request()`. A pid lock (`logs/bot.pid`) makes sure only one of them runs at a time.

| | **Scheduled mode** (deployed) | **Continuous mode** |
|---|---|---|
| Entry point | `paperbot.runtime.cycle --submit`, started by Windows Task Scheduler task `HT_Cycle` | `paperbot-bot --submit`, one long-running process |
| Loop trigger | `schtasks /SC MINUTE /MO 5`: a **new process every 5 minutes** on the :00/:05/:10… grid, around the clock | `ib.sleep(loop_seconds)`: **30 s after the previous pass finishes** |
| Activity gate | Exits in < 1 s on weekends and outside 09:30–16:05 ET | Loops at any hour. Outside the session it only protects positions |
| Entry-scan budget | 150 s per run (one run usually scans the whole list) | 10 s per loop, then resumes where it stopped on the next loop |
| Force close | From the first run at or after 15:51, it stays connected and **repeats the pass every 30 s** until flat or 16:00 | Every 30-s loop from 15:51 |

`scripts/start_paperbot.ps1` registers **scheduled mode**, so this document follows scheduled mode and notes where continuous mode differs.

---

## 1. Market open & initialization

### 1.1 What happens "the millisecond the market opens"

**Nothing happens at the opening bell itself.** Paperbot does not stream ticks and does not react to the opening print. Its first action of the session is the scheduled run that Task Scheduler starts at **09:30:00 ET**. Starting the Python process takes a few seconds, so that run actually begins around **09:30:02–09:30:05**. No entries are possible before **10:05 ET** (`earliest_entry_et`). This is deliberate: the strategy needs completed opening bars, today's watchlist (built at 09:55) and an RVOL reading.

### 1.2 Pre-open tasks (Task Scheduler, all Mon–Fri ET)

| ET | Task | Action |
|---|---|---|
| 07:00 (daily) | `HT_Reschedule` | Re-registers every task if the New York or PC UTC offset changed (DST). |
| 09:25 | `HT_LogRotate` | Moves earlier days' `logs/*.log` and `*.jsonl` files to `logs/archive/YYYY-MM-DD/`. Caps `safety_log.jsonl` at 5 MB and trims `data/trades.csv` to 90 days. Never touches state files. |

### 1.3 The 09:30 run: initialization sequence, step by step

Every scheduled run, including the first one of the day, runs these steps in order. A failure at any step ends the run, and **no order is sent**.

1. **Time gate (stdlib only, < 1 s).** It reads the ET clock. On a weekend, before 09:30 or at/after 16:05 it exits with code 0. It reads `force_close_et` (15:51) from `rules.json` and labels the run `run` (09:30 ≤ t < 15:51, or 16:00 ≤ t < 16:05) or `force_close` (15:51 ≤ t < 16:00).
2. **STOP file check.** If `STOP` exists in the project root, the run logs "cycle skipped" and exits.
3. **Single-instance check.** If `logs/bot.pid` points to a live process (continuous bot or an earlier cycle), the run skips.
4. **Load config.** It reads `config/config.json` (connection and hard caps) and merges in the `risk` section of `rules.json`. Where both files set the same key, `rules.json` wins.
5. **Load the risk-guard state, failing closed.** It reads `logs/state.json` (`date`, `orders_today`, `halted`, `halt_reason`). A corrupt file **stops the run** (`StateCorrupt`) rather than silently resetting today's counters. If the stored date is not today (ET), the state resets to `orders_today=0, halted=false`.
6. **Load the strategy state, failing closed.** It reads `logs/strategy_state.json`: per-position `initial_stop`, `stop`, `breakeven`, `partial_done`, `force_attempts`, plus `entered` (symbol → date of its last entry).
7. **Claim `logs/bot.pid`.**
8. **Connect** to IB Gateway `127.0.0.1:4002` with clientId `21`, the same id the bot always uses, because only the client that placed an order may modify it. On failure it waits 5 s and retries once. If both attempts fail, it exits 1, logs to `logs/cycle_errors.log` and sends an ERROR push.
9. **Verify paper.** The port must be 4002 or 7497, and IBKR must report **exactly one** managed account whose id starts with `D`. Anything else raises `SystemExit("Refusing to run…")`.
10. **Initial data fetches:**
    - `reqPnL(account)`: it waits up to 3 s (6 × 0.5 s) for the first `dailyPnL` value.
    - `reqAllOpenOrders()`: every open order on the account, so the bot sees its own `paperbot:*` orders.
    - Positions and account values (`NetLiquidation`) arrive with the connection's startup fetch. Executions are excluded at startup and requested explicitly in step 1 of the pass.
11. **Attach journals.** Every order-status change and fill is appended to `logs/orders.jsonl`.
12. **Restore per-process strategy settings.** Entry-scan budget = 150 s. Orphan-stop counters are loaded from `logs/cycle_state.json`, but only if they were saved less than 15 minutes ago.
13. **Run one pass** (§2.2).
14. Save the orphan counters, wait 2 s if any request was sent so IBKR can acknowledge it, **disconnect, release the pid file**.

### 1.4 Risk parameters loaded (live values)

| Source | Parameter | Value | Effect |
|---|---|---|---|
| `config.json → limits` | `allowed_symbols` | `["*"]` | Any symbol |
| | `max_quantity_per_order` | **1** | Hard share cap per entry. **This is the binding constraint today.** |
| | `max_order_value` | $2,500 | Cap on quantity × limit price per entry |
| | `max_orders_per_day` | 5 | Entries (BUY brackets) sent per ET day |
| | `max_open_orders` | 2 | Working entry + exit orders. Protective stops are not counted. |
| | `max_daily_loss` | $1,000 | Halts new entries for the day when IBKR `dailyPnL` ≤ −1,000 |
| | `limit_orders_only` | true | Entries and exits must be limit orders. Stops are STP. |
| | `time_in_force` | DAY | Entries and exits. Stops are always GTC. |
| `rules.json → risk` | `max_risk_per_trade_pct` | 1.0 % | (limit − stop) × qty ≤ 1 % of NetLiquidation |
| | `max_position_size_pct_of_portfolio` | 10 % | qty × limit ≤ 10 % of NetLiquidation |
| | `max_concurrent_positions` | 5 | Long positions plus symbols with a working BUY, **manual positions included** |
| `rules.json → time_filter` | entry window / force close | 10:05–15:30 / 15:51 | |
| `rules.json → exit` | stop / partial / breakeven / trail | LOD − 1 % / +0.75R, ⅓ / +1R / 5-min swing low (2, 2) | |

### 1.5 What the 09:30 run actually does

At 09:30:0x **no 5-minute RTH bar has completed yet**: the 09:30 bar completes at 09:35:00. So the first pass only:
- pushes any fills that happened while nothing was running (for example, an overnight GTC stop that filled pre-market),
- forgets state for positions that are gone, and re-adopts positions that still have a paperbot stop (§4.4),
- cancels orphan stops and makes sure every owned position has a correctly sized protective stop,
- writes `logs/heartbeat.json` and rebuilds `dashboard/index.html`.

From the **09:35** run onward, positions carried in from earlier (§5.3) get full exit management: breakeven, trailing stop and partial profit.

### 1.6 Watchlist build (09:55, re-run every 30 min until 12:55)

`HT_Prefilter_01…07` run `paperbot.market.morning_prefilter`, which never connects to IBKR:
1. Fetches today's S&P 500 members from Wikipedia.
2. Keeps the **100 largest by market cap** (Yahoo Finance, 5 threads).
3. Downloads 2 daily bars for those 100 in bulk.
4. Computes gap = today's open ÷ yesterday's close − 1. A symbol survives if gap ≥ 3 % and its latest price ≥ $3.
5. Sorts by gap, keeps the **top 20**, and writes them atomically to `data/watchlist.txt` with a header dated today.
6. Sends an ALERT push if ≥ 30 % of the symbols failed to download (≥ 95 % = connection failure). Exits 1 if Yahoo has no bar for today yet.

The strategy scans `watchlist.txt` only if its header date is today (ET). Otherwise it falls back to the static `config/universe.txt` (99 symbols) and logs a warning once per day. When a prefilter re-run changes the list, the scan position resets.

---

## 2. The core execution loop

### 2.1 Frequency and trigger

**Scheduled mode (deployed).** Task Scheduler starts a new process on a fixed 5-minute grid. Within one regular-session day:

| Window | Runs | Passes | Notes |
|---|---|---|---|
| 09:30 → 15:50 | 77 runs (09:30, 09:35 … 15:50) | 77 | One pass each |
| 15:55 (force close) | 1 run | **up to 10** | Repeats every 30 s until flat or 16:00 (15:55:0x, 15:55:3x … 15:59:3x). Ends early once flat. |
| 16:00 (post-close) | 1 run | 1 | Restores stops, raises the overnight ALERT |
| **Total** | **79 runs** | **≈ 79–88 passes** | Off-hours runs (all other :x0/:x5 slots) exit in < 1 s and do nothing |

Of those 79 runs, the **entry scan runs in 65** (10:05 → 15:25). Exit management for held positions runs in 76 (09:35 → 15:50).

**Continuous mode.** Loop time = pass duration + 30 s sleep. Upper bound for 6.5 h: 23,400 s ÷ 30 s = **780 loops**. A typical pass takes 1–12 s (longer when the entry scan uses its 10 s budget), so expect **about 600–750 loops** per session. The universe is still scanned **once per 5-minute bar** (65 bar-scans per day), spread over as many loops as the 10-s budget needs.

**Why 5-minute bars are enough.** Every decision uses the close of the **last completed 5-minute bar**. Protection doesn't wait for the loop: each stop is a real GTC order resting at IBKR and triggers on its own.

### 2.2 One pass, step by step

Each pass runs these steps in this exact order (`cycle.one_pass` / the body of `bot.main`'s loop):

1. **Daily-loss check.** Read `dailyPnL` (NaN counts as unavailable). If dailyPnL ≤ −$1,000, the guard sets `halted=true` with a reason. **On the transition to halted**, it cancels every working `paperbot:entry` and `paperbot:exit` order. Stops stay.
2. **Fill reconciliation.** `reqExecutions()` returns today's fills. New executions of `paperbot:*` orders (deduplicated by execId through `logs/notify_state.json`) are:
   - appended to `data/trades.csv`, with the initial stop on BUY rows so trades can be measured in R,
   - pushed once per order as `BUY` / `STOP` / `PARTIAL` / `EXIT`.
3. **`strategy.decide()` builds a list of requests.** Nothing is sent yet.
   1. Snapshot: long stock positions (`held`), the bot's open orders grouped by symbol and role (`stop`, `exits[]`, `entries[]`), and the strategy state.
   2. **Adopt / forget.** Drop state for symbols that are neither held nor have working orders. Re-adopt held symbols that have no state but do have a paperbot stop (§4.4).
   3. Classify the time: `session` = weekday and 09:30 ≤ t < 16:00. `force_close` = session and t ≥ 15:51.
   4. **Raise stops** (session, before force close). For each owned position whose entry is no longer working: fetch today's 5-minute bars, apply breakeven, then the swing-low trail (§5.1). This changes **state only**. Step 3.6 turns it into an order.
   5. **Cancel orphan stops.** A paperbot stop with no position and no working entry is counted. On the **second consecutive** pass it is cancelled, because it could otherwise open a short.
   6. **Reconcile stops.** For each owned position without a working entry: target qty = shares held − shares still on working exits. If no stop exists → ALERT and **re-create** it at `state.stop`. If the stop's qty ≠ target, or its price < `state.stop` → **modify** it to `max(state.stop, current stop price)`. Stops only move up. During force close, symbols being closed this pass are skipped.
   7. **Force close** (t ≥ 15:51): see §5.2.
   8. **Partial profit** (session, before force close): see §5.1.
   9. **Entry scan** (10:05:00 ≤ t ≤ 15:30:00): see §2.3.
   10. Save the strategy state atomically.
4. **Run each request through the risk guard, then IBKR, in list order.**
   - `CancelRequest`: cancel, then poll every 0.25 s for up to **5 s** for IBKR to confirm. If the cancel is **not confirmed, every later request for that symbol is skipped this pass**. A replacement SELL can therefore never sit next to a live stop and double-sell.
   - `StopUpdate`: `check_stop` (qty > 0, price > 0, qty ≤ shares held). An existing stop is **modified in place** (same orderId). Otherwise a new `SELL STP GTC` tagged `paperbot:stop` is placed.
   - `OrderIntent`: `check` (§3.3). A BUY goes out as a **bracket**. A SELL goes out as a `LMT DAY` tagged `paperbot:exit`.
5. **After the pass:** write `logs/heartbeat.json` (time, mode, positions with stop and last price) and rebuild the dashboard. Errors here are logged and never interrupt trading.

### 2.3 Entry scan detail

1. Symbol list: today's watchlist, or else `universe.txt`.
2. Skip the scan if `busy` (held symbols + symbols with a working entry) ≥ 5, or if NetLiquidation is unavailable.
3. Walk the list from the saved position until the time budget runs out, the STOP file appears, or `busy` + new signals reaches 5. Skip symbols that are busy or **already entered today**.
4. `evaluate(symbol)` runs the checks cheapest first and **stops at the first failure**. Every result, pass or fail with its reasons, is appended to `logs/safety_log.jsonl`:

| # | Check | Data | Rule |
|---|---|---|---|
| 1 | Contract qualifies | IBKR | `Stock(sym, SMART, USD)` gets a conId |
| 2 | Daily history | 1 Y daily RTH bars, bars before today | must exist |
| 3 | Min price (prior) | prior close | ≥ $3.00 |
| 4 | **D2** | prior close vs SMA200 of the last 200 closes | prior close > SMA200. Fewer than 200 days → fail |
| 5 | Bars | today's 5-min bars incl. pre-market, completed only (bar start + 5 min ≤ now) | ≥ 2 completed RTH bars |
| 6 | Min price (live) | `price` = close of last completed RTH bar | ≥ $3.00 |
| 7 | **D3** gap | 09:30 bar open ÷ prior close − 1 | ≥ 3.0 % |
| 8 | **D1** | price vs prior-day high | price > prior high |
| 9 | **I1** | price vs max high of pre-market bars | price > pre-market high. No pre-market bars → fail |
| 10 | **I2** | price vs max high of RTH bars **excluding the last one** | price > today's earlier HOD (a new-high breakout) |
| 11 | **I3** RVOL | today's cumulative RTH volume through the last bar ÷ the 14-day average cumulative volume up to the same time of day | ≥ 2.0. Fewer than 14 history days → fail |
| 12 | Stop | LOD of today's RTH bars | `stop = round(LOD × 0.99, 2)` |

5. **Sizing** (`_size_entry`):
   - `limit = round(price × 1.002, 2)`: a marketable limit 0.2 % through the last close.
   - `per_share_risk = limit − stop`. If ≤ 0, no trade.
   - `qty = floor(min( NLV×1% ÷ per_share_risk , NLV×10% ÷ limit , max_quantity_per_order , max_order_value ÷ limit ))`. If < 1, the signal is skipped.

### 2.4 Worked example: NVDA, one session (live config)

**Setup.** The paper account's NetLiquidation is $1,000,000. NVDA's prior close is $180.00, prior-day high $182.40, SMA200 $151.20. Pre-market high is $187.10. NVDA opens at $186.30 (gap +3.50 %) and appears in the 09:55 watchlist.

**09:30 → 10:00 runs.** No position and no entries allowed yet. The passes only reconcile and write the heartbeat.

**10:05:03 run: entry.** The completed RTH bars are 09:30 … 10:00 (7 bars).

| Check | Value | Result |
|---|---|---|
| D2 | 180.00 > 151.20 | pass |
| Last close (10:00 bar) | **189.20** | ≥ 3 ✔ |
| D3 gap | 186.30 / 180.00 − 1 = **3.50 %** | ≥ 3 ✔ |
| D1 | 189.20 > 182.40 | ✔ |
| I1 | 189.20 > 187.10 | ✔ |
| I2 | HOD of the first 6 bars = 188.90; 189.20 > 188.90 | ✔ |
| I3 | 41.6 M shares today through 10:05 ÷ 17.8 M 14-day avg = **2.34** | ≥ 2.0 ✔ |
| LOD | 185.40 (09:35 bar) | stop = round(185.40 × 0.99, 2) = **183.55** |

Sizing: limit = round(189.20 × 1.002, 2) = **189.58**. Per-share risk = 189.58 − 183.55 = **6.03**.

| Constraint | Shares |
|---|---|
| 1 % risk: 10,000 ÷ 6.03 | 1,658.4 |
| 10 % size: 100,000 ÷ 189.58 | 527.5 |
| `max_quantity_per_order` | **1** |
| `max_order_value`: 2,500 ÷ 189.58 | 13.2 |
| **floor(min)** | **1 share** |

Risk guard (all pass): action/qty/limit valid · stop 183.55 > 0 and < 189.58 · not halted · dailyPnL −$120 (available, above −$1,000) · value $189.58 ≤ $2,500 · orders_today 0 < 5 · working entry/exit orders 0 < 2 · position value ≤ $100,000 · risk $6.03 ≤ $10,000 · concurrent 0 < 5.

Orders sent as one bracket:
- Parent: `BUY 1 NVDA LMT 189.58 DAY`, `orderRef=paperbot:entry`, `transmit=False`
- Child: `SELL 1 NVDA STP 183.55 GTC`, `orderRef=paperbot:stop`, `parentId=parent`, `transmit=True`, which sends both together

Bookkeeping: `orders_today = 1`. Strategy state gets `NVDA: {initial_stop 183.55, stop 183.55, breakeven false, partial_done false}` and `entered.NVDA = today`, so there is **no second NVDA entry today**.

Fill: 1 @ **189.35** at 10:05:04. IBKR activates the child stop.

**10:10 run.** The fill reconciliation pushes `BUY NVDA @ $189.35, stop $183.55, qty 1` and writes the ledger row.

> **R uses IBKR's `avgCost`, which includes commission.** With 1 share and IBKR's $1.00 minimum commission, avgCost = **190.35**. So:
> R = 190.35 − 183.55 = **6.80** · partial trigger (+0.75R) = **195.45** · breakeven trigger (+1R) = **197.15**.

**10:40 run.** Last close 195.90 ≥ 195.45, so the partial-profit rule fires. part = min(round(1 × 0.3333), 1 − 1) = **0**, which is < 1, so `partial_done = true` and **no order is sent**. A 1-share position cannot be split.

**11:15 run.** Last close 197.60 ≥ 197.15, so **breakeven**: stop = max(183.55, 190.35) = 190.35 (push `BE NVDA`). In the same pass, the swing-low trail scans backwards for the most recent pivot low whose low is lower than each of the 2 bars before it and no higher than each of the 2 bars after it. The 10:45 bar qualifies: low **193.10**, prior lows 193.80 and 194.20, next lows 194.00 and 195.10. 193.10 > 190.35 and < 197.60, so stop = **193.10** (push `TRAIL NVDA`). Stop reconciliation then sends `MODIFY STP → 1 @ 193.10`, same orderId.

**12:30 run.** A newer pivot low at **196.40** gets the stop modified to 196.40.

**Outcome A: stopped out.** At 13:42 NVDA trades at 196.40. IBKR triggers the stop, which becomes a market order, and it fills at **196.35** while no Paperbot process is running. The 13:45 run pushes `STOP NVDA exit $196.35, P&L +$5.00` (+$7.00 gross − $2.00 commissions). The ledger records the exit, and NVDA's state is dropped because it is neither held nor working. The dashboard's R (from fills) = (196.35 − 189.35) ÷ (189.35 − 183.55) = **+1.21R**.

**Outcome B: held into the close.** See the force-close walkthrough in §5.2.

**Variant: caps lifted** (`max_quantity_per_order` ≥ 527 and `max_order_value` ≥ $100,000). Same signal, so **527 shares** (the 10 % size limit binds). Say the fill is at 189.35 and avgCost ≈ **189.36** (commission is negligible per share). R = 5.81, partial trigger = 193.72, BE trigger = 195.17. At the partial trigger, part = min(round(527 × 0.3333), 526) = **176 shares**. Two requests are sent in this order: (1) `StopUpdate` shrinks the stop to **351 shares**, (2) `SELL 176 LMT round(close × 0.998, 2) DAY`. So stop + exit = 527 and the account can never go short.

---

## 3. Real-time validation & risk checks

### 3.1 Market-data sanity

| Check | Implementation | On failure |
|---|---|---|
| Only completed bars | A 5-min bar is used only when `bar_start + 5 min ≤ now`. The bar still forming is never used. | — |
| Enough intraday data | Entries need ≥ 2 completed RTH bars | Symbol fails (`safety_log`) |
| Request timeouts | Historical requests time out after 10 s and contract lookups for orders after 20 s. A timeout returns no data. | Symbol fails / order not placed (journal `blocked`). A stop that can't be placed → **ALERT** |
| Missing price at force close | No RTH bars for a held symbol | **ALERT**. That symbol is skipped this pass and retried next pass |
| Invalid account P&L | `dailyPnL` None or NaN | **All new entries blocked** ("daily P&L unavailable") |
| Missing account value | NetLiquidation unavailable | Entry scan skipped. The guard also blocks the BUY |
| Prefilter data quality | ≥ 30 % of symbols failed → ALERT, ≥ 95 % → connection ALERT. No bar for today → exit 1 | Bot falls back to `universe.txt` if no watchlist is dated today |
| **Stale-quote detection** | **Not implemented.** There is no check that the last bar is recent (e.g. a halted stock). | — |
| **Bid/ask spread check** | **Not implemented.** Paperbot never requests quotes (`reqMktData`). All prices are 5-minute bar closes. A wide spread just leaves the 0.2 % marketable limit unfilled. | — |

### 3.2 Account risk

| Check | Rule | When applied |
|---|---|---|
| Daily loss halt | IBKR `dailyPnL` (realized + unrealized) ≤ −$1,000 → `halted=true` until ET midnight. Persisted in `logs/state.json`. | Every pass (step 1) and inside every entry check. On the transition, working entries **and exits** are cancelled. |
| Risk per trade | qty × (limit − stop) ≤ 1 % NLV | Every BUY |
| Position size | qty × limit ≤ 10 % NLV | Every BUY |
| Concurrency | held longs ∪ working-BUY symbols < 5 (includes manual positions) | Scan pre-check and every BUY |
| Daily entry count | `orders_today` < 5 | Every BUY |
| Working orders | non-stop open orders < 2 | Every BUY |
| Paper-only | port ∈ {4002, 7497} and a single `D…` account | Every connect |
| **Max drawdown (multi-day)** | **Not implemented.** Only the per-day loss halt exists. | — |
| **Buying power** | **Not checked directly.** Sizing uses NetLiquidation, and the 10 % / $2,500 caps keep orders far inside paper buying power. | — |

**Exits and stops are never blocked by any of these.** A closing SELL or a stop is checked only for valid action, qty, price and **qty ≤ shares held**. So the bot can always get out, even while halted, and can never turn a long into a short.

### 3.3 Order-execution limits

Applied by `RiskGuard.check` to every `OrderIntent`:
1. Action is BUY or SELL. Qty is an integer > 0. The limit price is > 0, and a market order is rejected because `limit_orders_only`.
2. **Closing SELL**: must be SELL and qty ≤ shares held. *(No other check.)*
3. **Opening SELL**: always rejected (long only).
4. **BUY**: a stop is required, with 0 < stop < limit · not halted · daily P&L available · symbol allowed · qty ≤ 1 · value ≤ $2,500 · entries today < 5 · working entry/exit orders < 2 · plus the §3.2 sizing and concurrency limits.

Any failure: the order is **not sent**, a `BLOCKED …` warning is logged, and a `blocked` event goes to `orders.jsonl`.

**Re-entry and wash-sale handling.** No wash-sale logic exists (no 30-day loss lookback). The only re-entry control is **one entry per symbol per ET day** (`strategy_state.entered`), which also blocks re-buying a symbol after a same-day stop-out. On a paper account this has no tax effect. Before any live use, a 30-day loss-sale lookback would have to be added.

---

## 4. State management of open and pending orders

### 4.1 Order roles

Every Paperbot order carries an `orderRef`. The bot only ever reads, modifies or cancels its own orders. Manual orders and positions are never touched (positions without state or a paperbot stop are logged once as "not managed").

| Role | Type / TIF | Created by |
|---|---|---|
| `paperbot:entry` | BUY LMT, DAY (bracket parent) | Entry signal |
| `paperbot:stop` | SELL STP, **GTC** (bracket child, or standalone) | Bracket, or stop reconciliation |
| `paperbot:exit` | SELL LMT, DAY | Partial profit or force close |

### 4.2 What a new pass does to orders from earlier passes

| Order already working | New pass behaviour |
|---|---|
| **Entry (unfilled)** | **Left alone. Never re-priced.** While it works, stop reconciliation, stop raising and partial profit are all skipped for that symbol, and IBKR manages the bracket child. It counts toward `max_open_orders` (2) and concurrency (5). It goes away only through: a fill · the daily-loss halt · a STOP/kill · force close (**only if that symbol is already held**) · IBKR's DAY expiry at 16:00, which cancels the child stop with it. |
| **Protective stop** | **Modified in place** (same orderId) when the size or the strategy's stop changes. The price only ever goes **up**. It is never cancelled except: orphaned for 2 passes · replaced at force close · `paperbot-kill --all`. Survives bot exit, Ctrl+C, crashes and Gateway restarts. |
| **Partial-profit exit** | Left alone while younger than **60 s**. At ≥ 60 s it is **cancelled**. The next pass's reconciliation resizes the stop back to the full remaining position. **No retry**, because `partial_done` was already set when the order was sent. |
| **Force-close exit** | Left alone while younger than 60 s. At ≥ 60 s it is cancelled and **replaced** at a wider limit (§5.2). |
| **Halt / STOP transition** | `cancel_working` cancels every working entry and exit. Stops stay. |

**Cancel/replace safety:** every cancel waits up to 5 s for IBKR's confirmation. An unconfirmed cancel holds back all further requests for that symbol until the next pass.

### 4.3 Partial fills

| Situation | Handling |
|---|---|
| Entry partially filled, remainder still working | The symbol counts as having a working entry, so the bot defers entirely to IBKR's bracket. No stop modification happens until the entry is no longer working. |
| Entry partially filled, remainder cancelled or expired | On the next pass, stop reconciliation sees the stop qty ≠ held shares and **modifies the stop to the held quantity**. *(Known lag: up to one pass, ≤ 5 min in scheduled mode.)* |
| Partial-profit SELL partially filled, then cancelled at 60 s | Next pass: target stop qty = held shares − 0 working exits, so the **stop is resized to the shares actually left**. Stop price = max(state stop, current stop). |
| Force-close SELL partially filled | The next attempt cancels it and sells `int(position)`, the **actual remaining** shares. |
| Stop partially filled | The remainder keeps working at IBKR. Reconciliation sees qty = held, so nothing changes. |
| Fills spread across executions | Notifications group executions by orderId (VWAP price, summed shares). The ledger keeps one row per execution. A position closed in pieces counts as **one** round trip in `paperbot-perf`. |

Under the live config (`max_quantity_per_order = 1`), partial fills **cannot occur**.

### 4.4 Persistence and recovery

- `logs/strategy_state.json` and `logs/state.json` are written atomically (temp file + `os.replace`) and read **fail closed**.
- If the strategy state is lost, a held position with a working paperbot stop is **re-adopted** from it: `initial_stop = stop = stop price`, `breakeven = (stop ≥ avgCost)`, `partial_done = true`. This logs an ALERT.
- The orphan-stop counter survives between scheduled runs via `logs/cycle_state.json` (it expires after 15 min).

---

## 5. Lifecycle end: liquidation & closing mechanisms

### 5.1 Intraday exits (normal conditions)

All of these are evaluated each pass from 09:35 (first completed bar) up to the last pass before force close. Each uses the **close of the last completed 5-minute bar**. R = avgCost − initial_stop.

| Exit | Trigger | Action |
|---|---|---|
| **Protective stop** | Price trades at or through the stop | Fills **at IBKR, instantly, independent of the loop** (STP → market) |
| **Partial profit** | close ≥ avgCost + 0.75R, once per position | Shrink the stop to (qty − part), then `SELL part LMT close × 0.998 DAY`. part = round(qty × 0.3333), capped at qty − 1. If part < 1 there is no sale. |
| **Breakeven** | close ≥ avgCost + 1.0R, once | stop = max(stop, avgCost) |
| **Trailing stop** | After breakeven, each pass | stop = the most recent 5-min swing low (2 bars each side), if it is above the current stop and below price |

There is **no profit target** that closes the whole position. Winners stay open until the trailing stop is hit or the force close.

### 5.2 End-of-day timeline

| ET | Scheduled mode (deployed) | Continuous mode |
|---|---|---|
| **15:25** | **Last effective entry scan** | Scanning continues |
| **15:30:00** | `latest_entry_et` boundary (inclusive to the second). The 15:30 run starts a few seconds after 15:30:00, so it **does not scan**. | Last scan possible at exactly 15:30:00 |
| 15:30–15:51 | Exit management only. Entries placed earlier that are still working **can still fill**. | Same |
| **15:50** | Last normal run (15:50 < 15:51) | — |
| **15:51** | *(no run)* | **Force close starts** |
| **15:55** | **Force close starts.** The run stays connected and repeats every 30 s. | Continues |
| **15:58** | Every pass: **ALERT** for each owned position still open | Same |
| **16:00** | DAY entry/exit orders expire. Post-close run: restore stops and send the overnight ALERT. | Same, and the loop keeps running |
| 16:05 | `HT_Dashboard` → `paperbot-perf` daily summary push. Cycle gate closes. | — |

**What one force-close pass does, per owned position:**
1. From 15:58 on: ALERT "still open".
2. If an exit order is younger than 60 s → leave it working and move on.
3. Fetch today's bars. If there are none → ALERT and retry next pass.
4. Cancel every working exit and entry, then **cancel the protective stop**. Each cancel is confirmed, and if any is unconfirmed, step 5 is skipped for that symbol.
5. `force_attempts += 1`. Send `SELL <all shares held> LMT round(close × (1 − 0.002 × min(attempts, 5)), 2) DAY`.
6. On the first attempt of the day, push **"EOD Force Close — flattening N positions"** (high priority).
7. Scheduled mode only: after each pass, if no owned position remains, the run disconnects.

**Re-pricing schedule.** Because of the 60-s freshness rule, the order is replaced on about every second 30-s pass:

| Attempt | Scheduled (≈ET) | Continuous (≈ET) | Limit discount |
|---|---|---|---|
| 1 | 15:55 | 15:51 | 0.2 % |
| 2 | 15:56 | 15:52 | 0.4 % |
| 3 | 15:57 | 15:53 | 0.6 % |
| 4 | 15:58 | 15:54 | 0.8 % |
| 5+ | 15:59 | 15:55 → 15:59 | 1.0 % (cap) |

**Outcome B for the §2.4 NVDA position:** at 15:55:03, the last completed bar (15:50) closed at 199.80. The bot cancels the 196.40 stop (confirmed), then sends `SELL 1 NVDA LMT round(199.80 × 0.998, 2) = 199.40 DAY`. It fills at 199.78. The 15:55:33 pass finds no owned positions and the run ends. The 16:00 run pushes `EXIT NVDA @ $199.78, P&L +$8.43` (≈ +$10.43 gross − ≈ $2.00 commission). R = (199.78 − 189.35) ÷ 5.80 = **+1.80R**.

### 5.3 Overnight policy

Paperbot is **flat-by-design**. It never intends to hold overnight and never opens a position after 15:30. If the force close does not fill by 16:00:
- every pass at or after 15:58 sends an ALERT, and after the close the bot sends `ALERT <SYM> still open after the close; protective stop kept at IBKR`,
- the post-close pass's stop reconciliation sees the stop was cancelled during force close, logs an ALERT and **re-places a GTC stop** at the strategy's last stop price,
- next morning the position is protected by that GTC stop, managed from 09:35 (breakeven/trail), and force-closed again from 15:51.

Stops are held on IBKR's servers, so a position stays protected even if the PC, Gateway or bot is down.

---

## 6. Known gaps & behaviour to be aware of

These come from the code as it is today. They are listed so nobody reads the config values above as guarantees.

1. **Force close starts at 15:55 in scheduled mode, not 15:51.** The 15:50 run ends before 15:51 and the next run starts at 15:55. That leaves about 5 re-pricing attempts instead of about 9. Fix: add a 15:51 task, or let the 15:50 run wait for 15:51.
2. **The 15:30 run never scans.** The window ends at exactly 15:30:00 and the run starts a few seconds later, so the effective last scan is 15:25.
3. **Possible post-close race.** If IBKR has not yet expired the DAY force-close SELL when the 16:00 run reconciles, the stop target becomes 0 and **no stop is restored**. The 16:05 slot is gated closed, so the position could stay unprotected until 09:30 the next day. Fix: let 16:05 run a protect-only pass, or ignore exits whose TIF is DAY after 16:00.
4. **No protection while a force-close attempt is working.** Between the stop cancel and the SELL fill (up to about 60 s per attempt), the position has no stop.
5. **Unfilled entries linger until 16:00.** A BUY limit that never fills is not cancelled when the window closes (force close only cancels entries for symbols already held). It occupies one of the 2 `max_open_orders` slots and one concurrency slot all day, and can fill late, for example at 15:45.
6. **Partial profit is one-shot.** If the 60-s partial order does not fill, the partial is forfeited for that position.
7. **R levels include commission.** `avgCost` folds in the commission, so on tiny sizes (1 share) every trigger moves up by about $1/share.
8. **Prices are bar closes, not quotes.** No spread or staleness checks. The force-close limit can be up to about 5 minutes old, which the widening discount only partly compensates for.
9. **No multi-day drawdown limit, no buying-power query, no wash-sale logic** (§3.2, §3.3).
10. **Scheduled runs don't cache across processes.** Each 5-minute run re-downloads 1 year of daily bars (and RVOL history for symbols that get that far) for every symbol it evaluates.
11. **Brackets and the cancel/replace flow have only been tested against a fake IBKR connection** (README "Known limitations"). The first `--submit` sessions need close watching.

---

## Appendix A — Files touched during a day

| File | Written by | Purpose |
|---|---|---|
| `data/watchlist.txt` | prefilter | Today's top-20 gappers |
| `logs/state.json` | risk guard | `orders_today`, `halted` (fail-closed) |
| `logs/strategy_state.json` | strategy | Per-position stops/flags, `entered` (fail-closed) |
| `logs/cycle_state.json` | cycle | Orphan-stop counters between runs |
| `logs/notify_state.json` | bot | execIds already pushed today |
| `logs/orders.jsonl` | bot | Every sent, blocked, cancelled, status and fill event |
| `logs/safety_log.jsonl` | strategy | Every symbol evaluation with its reasons |
| `logs/bot.log` | all | Human-readable log, ET timestamps |
| `logs/heartbeat.json` | bot/cycle | Liveness and open positions for the dashboard |
| `data/trades.csv` | bot | Fill ledger (one row per execution) |
| `dashboard/index.html` | compute_perf | Live status page, rebuilt after each pass |

## Appendix B — Stop controls

| Action | Entries/exits | Protective stops |
|---|---|---|
| `STOP` file / `paperbot-stop` | Cancelled (continuous mode). Scheduled runs simply skip. | **Kept** |
| Ctrl+C | Left working | Kept |
| `paperbot-kill` | Cancelled | **Kept** |
| `paperbot-kill --all` | Cancelled | **Cancelled — positions unprotected** |
