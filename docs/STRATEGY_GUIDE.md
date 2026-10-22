# Paperbot Trading Strategy Guide

This guide explains what the current Paperbot implementation is configured to
do. It is based on the checked-in `config/rules.json` and the strategy, block,
risk, order, market-data, and backtest code. It describes software behavior,
not a recommendation to trade. Strategies can lose money; a backtest or a
qualifying signal does not establish that a live trade will be profitable.
The checked-in runtime configuration currently selects **paper mode**
(`config/config.json`, port 4002), not live mode.

All worked price paths below are **hypothetical simulations**, not historical
price claims, actual Paperbot signals, or recorded fills. They use symbols from
the configured universe where practical. The examples omit costs unless
explicitly stated; actual execution prices, commissions, and slippage can
change the results.

## 1. What is implemented today?

Paperbot has one generic, configurable strategy engine rather than one
independent algorithm class per strategy. Each entry in `config/rules.json` is a
profile made of registered entry, stop, management, and exit blocks. The
current configuration contains these three executable profiles:

| Strategy key and status | Intended behavior and market | Entry and holding period | Stop and exits | Universe and verification |
|---|---|---|---|---|
| `trend_join_long` — implemented | Long intraday momentum in stocks showing a strong upward gap and breakout. It depends on follow-through after a gap, not merely a rising long-term trend. | 5-minute bars; eligible 10:05–15:30 ET; intraday only. | Initial stop 1% below the regular-hours low; partial sale at +0.75R; stop moved to entry at +1R; then swing-low trailing; force-close from 15:51 ET. | Current-day S&P 500 watchlist, or `config/universe.txt` fallback. `tests/test_strategy.py`, `tests/test_risk_policy.py`. |
| `macro_swing_etf` — implemented | Long multi-day pullbacks in sector ETFs that remain above their long-term trend. It expects a short-term rebound after a sharp decline. | Daily indicators, checked 15:45–15:55 ET; holds overnight. | Initial stop 2 × ATR(14) below the signal price; no partial/trailing management blocks. Exit checks at/after 15:50 ET on days after entry: above SMA(5), or held 10 trading days. The protective stop remains GTC. | Exactly `XLK`, `XLF`, `XLE`, `XLV`, `XLI`, `XLY`, `XLP`, `XLU`, `XLB`, `XLRE`, `XLC`. `tests/test_macro_swing.py`. |
| `equity_mean_reversion` — implemented | Long intraday rebounds in broad-market ETFs after a large downside stretch from the session's volume-weighted average price. It can fail badly if the decline is a genuine trend. | 1-minute bars; continuous mode only; eligible 10:00–15:30 ET; up to 3 entries per symbol per ET date. | Stop 1 session VWAP standard deviation below the entry reference; exit at/above VWAP, after 30 minutes, or force-close from 15:50 ET. | Exactly `SPY`, `QQQ`, `IWM`. `tests/test_mean_reversion.py`. |

Experiments under `backtesting/experiments/` are proposed rule variations,
not additional production strategies. The backtester discovers the current
strategy profiles from `rules.json`; adding a profile makes it available for
simulation, but only profiles that pass validation and are selected for a live
session can trade. The repository currently selects `trend_join_long` as its
default.

The parameter values below are the explicit current values in `rules.json`.
Code defaults are used only when a profile omits an optional block parameter;
they do not override the explicit values shown here. Example values in tests
are not production settings.

## 2. Terms and decision flow

- A **bar** summarizes a period's open, high, low, close, and volume. A
  completed 5-minute bar does not become usable until its five minutes have
  elapsed. A **daily bar** summarizes a trading day.
- A **simple moving average (SMA)** is the arithmetic average of the latest
  configured number of daily closes.
- **RSI(2)** is a 0–100 measure of recent gains versus losses. It is seeded
  from the first two daily changes and then Wilder-smoothed (period 2) across
  the whole available daily series (about a year), so the latest one or two
  changes dominate. A low value means recent closes fell sharply; it does not
  itself predict a rebound.
- **ATR(14)** measures recent daily price range, including gaps from the
  previous close. It is used to set a stop distance; it does not cap the loss
  if a price gaps through a stop.
- **VWAP** is a session average price weighted by traded volume. Paperbot
  calculates it from each intraday bar's typical price `(high + low + close)/3`.
  The session's volume-weighted standard deviation (`sd`) measures how far
  those typical prices have varied around VWAP.
- **R** is the per-share risk between entry and initial stop. It is measured
  two ways. For sizing and the pre-trade risk check, R is the entry **limit**
  minus the stop. Once a position is open, partial-profit and breakeven
  triggers use IBKR's **average cost** of the fill (which includes the entry
  commission) minus the initial stop. For example, an entry filled at $100
  with a $98 stop has $2/share of initial risk; +1R is $102. Because a
  marketable limit usually fills at or below its limit, the live triggers are
  typically a little lower than limit-based figures. R is a planning measure,
  not a guarantee of maximum loss.
- **RVOL** is volume accumulated so far today divided by the average volume
  accumulated by the same time over the prior 14 sessions.
- A **signal** means every configured entry block passed. It is not an
  approved order, an accepted order, or a fill.
- A **marketable limit** is a limit price set across the current quote in an
  attempt to fill promptly. It can still fail to fill.
- An **OCA group** links a stop and an exit so the broker reduces/cancels the
  other order as shares are sold. Full signal/force-close exits use this link;
  partial-profit handling first resizes the protective stop before sending the
  partial SELL.

The live decision path is:

1. The selected profile and all configured blocks and risk limits are validated
   before connecting to IBKR.
2. Paperbot reads its own positions and working orders, reconciles protective
   stops, manages/considers exits, and checks the profile's entry window.
3. It scans the configured universe. Entry blocks run in their listed order;
   the first failure rejects that symbol for that evaluation. A block error
   also skips that symbol and is logged.
4. Entry signals use completed bars. For intraday profiles the price signal is
   the latest completed bar close; for the daily profile today's price is the
   live bid/ask midpoint. A two-sided live quote is then required to price an
   entry.
5. The sizing code creates whole-share quantities and rounds prices to a
   $0.01 tick. The BUY limit is 0.2% above the live ask (rounded up). It sizes
   to the tightest configured constraint, including risk, portfolio value,
   order value, share cap, buying power, and estimated commission drag.
6. The risk guard independently checks the order, including the account's
   buying power, daily loss and drawdown state, strategy limits, existing
   positions, order counts, allowed symbols, and protective stop. A blocked
   order is journaled; a signal alone does not place it.
7. An approved BUY is sent as a bracket: a GTD limit BUY that IBKR expires one
   minute after the entry window, with a child GTC protective stop. (The
   `time_in_force: DAY` setting in `config.json` applies to closing SELLs.) The stop activates if the parent fills.
   The entry expires after the configured entry window; an unfilled order is
   not a position.
8. Paperbot reconciles the position against IBKR, manages the stop, and sends
   closing limit SELLs for exit signals. Exit orders are repriced if still
   working. A stop remains live while a corresponding exit is working in its
   OCA group. P&L is based on fills, not the signal's reference price.

The normal live service checks every `loop_seconds` (currently 30 seconds).
The entry universe is scanned once per strategy bar, but at least every five
minutes: the 1m profile scans every minute, the 5m profile every five minutes,
and the 1d profile every five minutes inside its window. A scan that exceeds
its time budget resumes on the next pass. Legacy scheduled
passes run every five minutes. The mean-reversion profile is marked
continuous-only and is refused in scheduled mode. Exits and stop reconciliation
are shared engine behavior, not separate strategy algorithms. See
`src/paperbot/strategies/rule_strategy.py`, `src/paperbot/blocks/`,
`src/paperbot/core/risk.py`, `src/paperbot/core/sizing.py`, and
`src/paperbot/core/orders.py`.

## 3. Strategy details

### 3.1 `trend_join_long` — Trend Join Long

**Idea and assumptions.** This is a long-only intraday breakout profile. It
looks for a stock whose price has gapped upward, is breaking above several
reference highs, and is trading with unusually high volume. It assumes that
some strong gaps continue rather than immediately reverse. A gap can also be
news-driven, temporary, or already exhausted by the time all checks pass.

**Exact entry checks** (all required, in the order configured):

1. Previous daily close at least **$3** (`min_prior_close`).
2. Previous daily close above its **200-day SMA** (`prior_close_above_sma`).
3. Latest completed 5-minute close at least **$3** (`min_price`).
4. Today's regular-hours opening price at least **3% above** yesterday's
   close (`min_gap_pct`).
5. Latest completed close above the previous day's high.
6. Latest completed close above the highest premarket high.
7. Latest completed close above the high of every earlier completed
   regular-hours bar today.
8. RVOL at least **2.0**, compared at the same time of day against **14**
   prior sessions.

If any check fails (including absent premarket bars, fewer than two completed
regular-hours bars, or unavailable required RVOL history), that symbol is
skipped for that evaluation. Failed reasons go to the strategy safety log.
Signals are evaluated once per 5-minute bar. The profile uses today's
watchlist if its date and index match; otherwise it scans the configured
fallback list. It does not guarantee that a symbol stays above the breakout
level until the order can be sent or filled.

**Stop, management, and exit.** The initial long stop is the regular-hours
low so far minus **1%**, rounded down to the valid tick. The live position
manager watches completed 5-minute closes:

- At **+0.75R** (R from the average fill cost), it attempts to sell **33.33%**
  of shares (nearest whole share, always leaving at least one), priced from the
  live bid. It shrinks the stop quantity first. A partial order still unfilled
  after 60 seconds is cancelled; there are at most **three attempts in total**
  (the first plus two retries), then it gives up the partial.
- At **+1R**, it raises the stop to at least the average fill cost (which
  includes the entry commission; exit commission and slippage are not covered).
- After breakeven, it can raise the stop to the latest swing low using two
  bars on the left and two on the right. A confirmed pivot therefore requires
  later bars; stops only move up.
- At/after **15:51 ET**, it attempts to flatten the remaining shares using a
  marketable limit SELL, repricing once the working order is 60 seconds old
  and widening the limit 0.2% below the bid per attempt (capped at 1% from
  the fifth attempt). From 15:58 it logs an alert if
  still held. The GTC stop remains protective while the close is attempted.

There is no separate opposite-signal exit block. Force close is the configured
signal-independent end-of-day exit. A stop can trigger at any time, including
outside the normal entry scan. If a closing quote is unavailable, Paperbot can
fall back to the last available bar close and logs an alert.

**Configuration and parameter effects:**

| Parameter | Current value | Meaning | If increased / decreased |
|---|---:|---|---|
| `timeframe` / entry window | 5m / 10:05–15:30 ET | Signal bars and inclusive entry window. | A slower bar can delay/omit intraday signals; a wider window permits more late-day entries, a narrower one fewer. |
| Previous close / minimum price | $3 / $3 | Both prior close and latest signal close must meet the floor. | Raising either floor excludes more low-priced stocks; lowering includes more. |
| Prior-close SMA | 200 days | Previous close must be above the long-term average. | Longer period changes the trend reference more slowly; shorter reacts faster. |
| Minimum gap | 3% | Today's regular-session open relative to yesterday's close. | Raising it requires a larger gap and fewer candidates; lowering admits smaller gaps. |
| RVOL | 2.0 over 14 sessions | Today's same-time cumulative volume relative to prior sessions. | Raising 2.0 demands stronger relative activity; increasing lookback smooths history but needs more data. Lowering either makes the volume filter easier to pass. |
| Initial stop | 1% below regular-hours low | Stop distance is anchored to the day's lowest low, not entry price. | Raising the percentage places the stop farther below the low; lowering tightens it. A farther stop generally reduces share size under the risk cap. |
| Partial profit | +0.75R; 33.33% | Price trigger and fraction of shares to sell. | Higher R waits for more favorable movement; larger fraction sells more shares, leaving fewer for later moves. Lower values trigger earlier/sell less. |
| Breakeven / swing trail | +1R; 2 bars left and right | Trigger for stop-to-entry; pivot lookback for subsequent trailing. | Higher breakeven R waits longer. More swing bars require a more established pivot and can trail less responsively; fewer bars react sooner. |
| Force close | 15:51 ET | Starts flatten attempts; not an entry time. | Later gives more holding time but less close buffer; earlier reduces exposure time and increases forced exits. |

**Representative trade lifecycle:** (1) A completed 5-minute close passes all
eight checks. (2) Paperbot prices a whole-share BUY from the live ask and
checks the profile/account limits. (3) If approved, it sends the BUY with a
GTD expiry and protective stop; only a fill creates a position. (4) Completed
bar closes can trigger partial profit, breakeven, and then a swing-low trail.
(5) The protective stop or 15:51 force-close mechanism reduces/closes shares;
reported P&L comes from the fills.

**Configured risk limits** (`rules.json`, not the examples in this guide):

| Setting | Current value | Practical effect |
|---|---:|---|
| Risk per trade | 1.0% of net liquidation | Caps intended entry-to-stop loss; a gap can exceed it. |
| Position value | 10% of net liquidation | Caps the planned share value. |
| Concurrent positions | 5 | Prevents additional new symbols above the limit. Counts every long stock position in the account (including manual ones) plus symbols with a working BUY. |
| Estimated round-trip commissions | At most 1% of R | Skips trades whose estimated fees are too large relative to planned risk. |
| Max drawdown breaker | $6,000 | Trips a separate entry-blocking breaker; manual reset is required in live behavior. |
| Quantity / order value | 500 shares / $30,000 | Per-order caps. |
| Entries / open orders | 5 per ET day / 2 open orders | Stops excess entry traffic. The open-order count covers every open non-stop order in the account (this strategy's entries and exits, but also manual orders and other strategies' orders); paperbot protective stops are not counted. It only blocks new BUYs. |
| Daily loss | $2,000 | Halts new entries when IBKR daily P&L reaches the threshold. At the moment of the halt, the bot cancels its working entry **and exit** orders (stops stay); new exits and stop updates are still allowed afterwards, and a decided exit is re-sent on a later pass. |

**Three hypothetical scenarios** (not actual AAPL or other historical data):

1. **Favorable continuation.** Assume a watchlisted stock closed at $100,
   its 200-day SMA is $95, its prior high is $102, and premarket high is
   $105. The stock opens at $104 and, after the opening bars, a completed
   5-minute bar closes at $106.50 with today's high now above all earlier
   highs and RVOL 2.4. If all other checks pass, the signal is valid. If the
   live ask is $106.60, the entry limit is approximately $106.82 after the
   0.2% offset and tick rounding. With an initial stop at $100, sizing uses
   a planned per-share risk of $6.82. If the order then fills at the $106.60
   ask (ignoring the small commission included in IBKR's average cost), the
   live R is $6.60: the +0.75R partial threshold is about $111.55 and the +1R
   trigger about $113.20, at which point the stop may be raised to about
   $106.60. Neither threshold guarantees a fill or a net profit.
2. **False breakout and reversal.** Assume the same checks pass, but a sharp
   reversal starts before or just after the entry fills. A stop at $100 may
   eventually sell the shares, but a gap through it can fill lower. The actual
   loss can exceed the calculated per-share risk; the stop is a trigger, not
   a guaranteed price.
3. **Looks strong but is rejected.** Price closes above the previous-day and
   premarket highs, but RVOL is 1.7, or there are not 14 comparable history
   days. The final volume block rejects the symbol (unknown RVOL also fails).
   Paperbot does not submit an entry merely because the breakout looks
   convincing.

**Main failure modes.** The high bar of conjunctive filters reduces the
number of signals but cannot eliminate false breakouts. Opening gaps and
overnight news can make an initial stop much farther away than expected;
the 1%-below-day-low rule can result in a wide stop and smaller share size.
The stop may not fill at its stop price during a gap or halt. A missing
watchlist can switch the scan to the fallback universe, which may differ from
the intended current-day candidates. Low-quality/stale volume history can
make the RVOL comparison misleading; live IBKR bars and history are required.

### 3.2 `macro_swing_etf` — Macro Swing ETF

**Idea and assumptions.** This is a long-only pullback strategy in 11 sector
ETFs. It wants a sector ETF to remain above its 200-day trend average while
its latest two daily changes show an extreme pullback (RSI(2) below 10). It
assumes that a short-term decline inside a longer-term uptrend may rebound.
It is not a market-wide macro forecast and it does not short a weak sector.

**Exact entry checks.** During **15:45–15:55 ET**, the profile evaluates
`XLK`, `XLF`, `XLE`, `XLV`, `XLI`, `XLY`, `XLP`, `XLU`, `XLB`, `XLRE`, and
`XLC`. It uses completed daily history before today plus today's live quote
midpoint as today's close:

1. The midpoint must be above the 200-day SMA calculated over that augmented
   daily-close series.
2. Wilder RSI(2) of the same series must be **below 10.0**.
3. ATR(14) must be available from completed daily bars; otherwise the stop
   cannot be computed and the candidate is skipped.

The entry order uses the live ask, not the midpoint, and expires one minute
after the entry window closes. The daily profile is rechecked every five
minutes during the window. It can therefore use a different live midpoint on
each pass; one daily bar alone does not freeze the signal.

**Stop and exits.** The initial stop is today's signal midpoint minus **2 ×
daily ATR(14)**, rounded down. There are no configured partial-profit,
breakeven, or trailing-stop blocks. IBKR holds the protective stop GTC
overnight. On days after entry, from **15:50 ET**, the first configured exit
that fires is retained and Paperbot sends a closing SELL in the stop's OCA
group:

1. Exit if the live midpoint is above the **5-day SMA** calculated from prior
   daily closes plus today's midpoint.
2. Otherwise exit after **10 trading days** held. Weekends and holidays are
   not counted. Entry-day exits are explicitly suppressed.

The exit order is repriced if it does not fill. This profile has no
force-close time; positions can remain overnight. If the exit cannot fill by
market close, the decided exit is retained into the next session while the
GTC stop remains.

**Configured risk limits:**

| Setting | Current value | Practical effect |
|---|---:|---|
| Risk per trade / position value | 1.0% / 10% of net liquidation | Caps planned risk and position value. |
| Concurrent positions | 3 | Limits the number of held/new symbols, counted across the whole account (manual positions included). |
| Estimated round-trip commissions | At most 1% of R | Rejects a trade if the fee estimate is too high. |
| Max drawdown breaker / daily loss | $6,000 / $2,000 | Blocks new entries after breaker or daily-loss trip. The trip cancels working entry and exit orders once; exits are re-sent afterwards and stops stay. |
| Quantity / order value | 1,000 shares / $30,000 | Per-order caps. |
| Entries / open orders | 5 per ET day / 3 open orders | Limits entry count and open orders. The open-order count includes every open non-stop order in the account, manual ones included; protective stops are excluded. Only new BUYs are blocked. |

**Configuration and parameter effects:**

| Parameter | Current value | Meaning | If increased / decreased |
|---|---:|---|---|
| `timeframe` / entry window | 1d / 15:45–15:55 ET | Daily indicators, rechecked on the five-minute pass grid during the closing window. | A later/narrower window uses a more current midpoint but gives less time for entry fills; an earlier/wider one may use less representative closing information. |
| Universe | 11 named sector ETFs | The only symbols scanned. | Adding symbols diversifies candidates but can add correlated positions; removing one excludes it. |
| Trend filter | Price > SMA(200) | Long-term trend eligibility. | Longer SMA changes more slowly; shorter reacts faster. |
| Pullback filter | RSI(2) < 10 | Extreme short-term weakness, calculated with today's live midpoint. | Raising 10 admits milder pullbacks/more candidates; lowering it requires more extreme weakness/fewer. Increasing period smooths the measure; decreasing makes it more reactive. |
| Initial stop | 2 × ATR(14) below signal midpoint | Volatility-scaled protective stop. | Raising the multiple/period generally widens or changes the stop and reduces size under the risk cap; lowering the multiple tightens it and may increase stop-outs. ATR period changes responsiveness, not monotonically the current ATR. |
| SMA exit | Price > SMA(5), from 15:50 ET | Exit test uses daily history plus today's live midpoint. | Longer average generally delays/reduces some exits; shorter responds more quickly. Moving the time later reduces the chance of an earlier exit but gives less time to fill. |
| Time stop | 10 trading days, from 15:50 ET | Maximum intended holding period before exit. | More days permits longer holds and overnight exposure; fewer days forces earlier exits. |

**Representative trade lifecycle:** (1) In the closing window, today's live
midpoint passes the SMA200 and RSI(2) filters; ATR(14) supplies a stop
distance. (2) The ask-based BUY limit is sized and risk-checked. (3) If filled,
the broker-held GTC stop protects the long position overnight. (4) On a later
trading day, at/after 15:50, the SMA(5) exit is checked before the 10-day time
stop. (5) A triggered exit is retained and repriced until filled; if not
filled, the position can remain open with its stop.

**Three hypothetical scenarios:**

1. **Favorable rebound.** Assume XLK's daily close series is above its SMA200,
   RSI(2) including today's $200.00 midpoint is 8, and its prior-bar ATR(14)
   is $2.00. The signal stop is $196.00. With a hypothetical $200.20 ask,
   the entry limit is about $200.61. If the next session later rises above
   its then-current SMA5 at/after 15:50, Paperbot decides to exit and submits
   a marketable SELL. Net return depends on actual fill prices and fees.
2. **Gap down through the stop.** Assume the position has a $196 stop but
   overnight news sends the next open to $190. The protective stop can execute
   below $196; a $6 gap per share already exceeds the planned $4 midpoint-to-
   stop distance. The loss can be larger still after costs.
3. **Partial conditions / changing signal.** Assume the ETF is above SMA200,
   but RSI(2) is 10.0 or higher. The strict `below` condition fails. If it
   later falls below 10 on a subsequent pass in the window, it may qualify;
   a brief earlier intraday dip does not count if the live midpoint has
   already recovered. Missing live two-sided quote or insufficient indicator
   history blocks the entry rather than inventing a value.

**Main failure modes.** RSI(2) below 10 can persist in a sustained selloff;
being above SMA200 does not make the pullback safe. The stop is based on a
historical volatility estimate and cannot cap overnight gap risk. A live
midpoint late in the session is only a proxy for the eventual daily close.
The live entry window is 10 minutes; a signal/order can be stale or expire
unfilled. In the backtest, daily decisions are driven by 5-minute bars and
the latest completed 5-minute close, which approximates rather than exactly
reproduces a live quote.

### 3.3 `equity_mean_reversion` — Equities Mean Reversion

**Idea and assumptions.** This long-only intraday strategy looks for SPY, QQQ,
or IWM to trade unusually far below the day's volume-weighted average and
tries to capture a return toward that average. The assumption is that some
sharp intraday declines are temporary. A real market-wide selloff can keep
moving down instead; the signal does not distinguish a temporary overreaction
from new information.

**Exact entry checks.** From **10:00–15:30 ET**, on completed 1-minute
regular-hours bars, the latest close must be at or below:

`session VWAP - 2.0 × volume-weighted session standard deviation`

The session bands use all completed regular-hours bars available at the
evaluation. The entry block rejects a missing/zero-volume VWAP or deviation.
No other confirmation (e.g. RSI, trend filter, or volume threshold) is
configured. The profile can re-enter a symbol up to **three times per ET
date**, after earlier position/order state allows it.

**Stop and exits.** The initial stop is the latest completed close minus
**1.0 × the same session VWAP standard deviation**, rounded down. Exits are
checked in configured order:

1. At/above today's VWAP using a live midpoint: exit for the mean target.
2. After **30 minutes** from the recorded entry time: time exit. That time is
   when the BUY was sent, not when it filled, so a slow fill leaves less than
   30 minutes in the trade.
3. From **15:50 ET**: force-close all owned positions.

The force-close mechanism is handled specially by the shared order manager.
Signal exits are retained and repriced if not filled; the stop remains linked
through OCA. This strategy has no breakeven or trailing management blocks.

**Configured risk limits:**

| Setting | Current value | Practical effect |
|---|---:|---|
| Risk per trade / position value | 0.5% / 10% of net liquidation | Half the per-trade risk allowance of the other two profiles; position-value cap remains 10%. |
| Concurrent positions | 2 | Limits held/new symbols, counted across the whole account (manual positions included). |
| Estimated round-trip commissions | At most 5% of R | Higher fee allowance than the other profiles, specifically to permit tight-stop ETF scalps; costs still reduce P&L. |
| Max drawdown breaker / daily loss | $6,000 / $2,000 | Blocks new entries when tripped. The trip cancels working entry and exit orders once; exits are re-sent afterwards and stops stay. |
| Quantity / order value | 500 shares / $30,000 | Per-order caps. |
| Entries / open orders | 5 per ET day / 2 open orders | Limits order traffic. The open-order count includes every open non-stop order in the account, manual ones included; protective stops are excluded. Only new BUYs are blocked. |
| Per-symbol daily entries | 3 | Applies in addition to the global daily order cap. |

**Configuration and parameter effects:**

| Parameter | Current value | Meaning | If increased / decreased |
|---|---:|---|---|
| `timeframe` / mode | 1m / continuous only | One-minute completed bars; scheduled mode is disallowed. | Longer bars smooth/delay the signal; shorter bars react faster but may be noisier. |
| Universe / entry window | SPY, QQQ, IWM / 10:00–15:30 ET | Fixed ETF list and inclusive entry times. | Adding symbols adds candidates and correlated exposure; shortening the window reduces opportunities, extending it permits later signals. |
| Entry stretch | 2.0 VWAP standard deviations | Close must be at/below VWAP minus this multiple. | Raising the multiple demands a more extreme drop/fewer entries; lowering it admits more ordinary declines. |
| Stop distance | 1.0 VWAP standard deviation | Initial stop below the signal close. | Raising the multiple widens intended risk and usually reduces share size; lowering tightens the stop and can increase noise stop-outs. |
| Target / time stop | VWAP / 30 minutes | Exit at/above session VWAP or after 30 minutes. The target level is fixed by the `target_vwap` block. | The target has no threshold parameter in the current block. More time-stop minutes holds longer and risks more adverse movement; fewer exits sooner. |
| Force close | 15:50 ET | Starts flatten attempts. | Later leaves less time to complete before market close; earlier closes positions sooner. |
| Daily entry cap | 3 per symbol | Caps re-entries after an earlier position/order state allows it. | Higher permits more repeated exposure; lower suppresses re-entry sooner. |

**Representative trade lifecycle:** (1) A completed 1-minute close reaches
the two-sigma downside threshold. (2) Paperbot calculates a stop from that
close, then prices and risk-checks a whole-share entry using the live ask.
(3) If the bracket BUY fills, the protective stop activates. (4) A live
midpoint at/above VWAP, 30 minutes held, or the 15:50 force-close phase
initiates an exit. (5) Actual fills determine whether the move made money
after spread, slippage, and commissions.

**Three hypothetical scenarios:**

1. **Reversion reaches target.** Assume SPY's completed-bar VWAP is $500.00
   and its deviation is $1.00. A completed close at $498.00 meets the
   `VWAP - 2sd` threshold. The initial stop is $497.00. If the next eligible
   entry order fills and a later live midpoint reaches $500.00, the target
   exit fires. The gross reference move is about $2/share before entry/exit
   execution costs and commissions.
2. **Downtrend continues.** Assume the same $498 signal and $497 stop, but
   SPY continues falling. It may hit the stop quickly; if it gaps below the
   stop, the realized loss can exceed $1/share. A 1-sigma stop is not a
   guarantee that the price will revert or that the loss is limited to one
   standard deviation.
3. **Threshold touched on a bar but not at evaluation.** A live tick briefly
   dips below the two-sigma line, but the completed 1-minute close is $498.30
   while the threshold is $498.00. No entry signal is generated from that
   transient touch. If the condition is satisfied but the required two-sided
   quote is unavailable, sizing cannot produce an entry order. There is no
   configured low-volume filter; an apparently extreme move on thin data can
   still qualify if the bar/VWAP calculations are valid.

**Main failure modes.** This is exposed to falling-market risk and can
repeatedly buy dips (within its three-entry cap) during a selloff. VWAP bands
are computed from bars, not top-of-book data, and the VWAP level can move as
new volume arrives. A small standard deviation creates a tight stop and
commission drag; if costs exceed the configured risk fraction, sizing skips
the trade. There is no configured market-regime, news, volatility-spike, or
minimum liquidity filter beyond valid market data and account/order limits.

## 4. Shared limits, orders, and exit precedence

The profiles are not run as three simultaneous capital pools in the regular
bot. A session selects one profile; its order references and per-position
strategy state are isolated. The account-level risk state is **shared** by
every strategy: the daily entry count and daily-loss halt
(`logs/state.json`) and the drawdown breaker (`logs/drawdown_state.json`).
Switching strategies mid-day therefore keeps today's entry count and any
halt. The backtest can evaluate profiles independently or as a suite, but
that does not mean a live account runs all three concurrently.

The following common risk-limit interpretations apply to the current values
shown in each strategy's risk table:

| Risk parameter | Effect of increasing it | Effect of decreasing it |
|---|---|---|
| Risk per trade / position value | Permits more planned loss / larger notional positions (subject to all other caps). | Tightens sizing and can reduce or eliminate entries. |
| Concurrent positions | Permits more symbols to be held or have an entry working. | Blocks new symbols sooner. |
| Maximum commission as % of R | Permits trades where fees consume a larger share of planned risk. | Rejects more fee-heavy/tight-stop trades. |
| Maximum drawdown / daily loss | Allows a larger loss before the respective breaker/entry halt. | Halts new entries earlier; does not itself liquidate existing holdings. |
| Quantity / order value | Permits larger individual orders. | Caps or blocks larger orders. |
| Orders per day / open orders | Allows more daily entries / working orders. | Limits entry throughput; open-order cap excludes protective stops. |

In each case, increasing one limit does not override the other limits or
broker buying power. Configuration changes to risk settings are also pinned by
`tests/test_risk_policy.py`, which requires deliberate test approval.

For all profiles:

- Whole-share quantity is floored to the tightest applicable cap. If even one
  share exceeds buying power, size/value caps, or risk constraints, the
  candidate is skipped.
- New BUY orders need a valid stop below their limit. The default configured
  order policy requires limits. Risk checks also validate daily loss, order
  count, open-order count, concurrent positions, commission drag, account
  buying power, and configured allowed symbols. Unknown daily P&L or buying
  power blocks entries when a check requires it.
- Entries are long-only. A current position, a working entry for that symbol,
  or the per-symbol daily entry count can suppress a duplicate entry.
- Closing SELLs and protective stop updates are not blocked by entry-only
  limits or a daily-loss halt, but are constrained not to sell more shares
  than held. When a halt first trips, the bot cancels its working entry and
  exit orders once (stops stay); a decided exit is then re-sent on a later
  pass.
- The open-order and concurrent-position caps look at the whole account:
  manual orders and positions, and other strategies' orders, count toward
  them. The bot primarily manages its own tagged positions; a manual
  holding without Paperbot state/protective stop is left unmanaged.
- The code checks configured signal exits in list order; the **first** one
  that fires is retained while it is repriced. The protective stop may execute
  before a signal exit fills. For intraday profiles, the force-close phase
  takes precedence over regular signal exits from its configured time.
- Protective stops are not guaranteed-price stop orders. Gaps, halts, and
  unavailable liquidity can result in a worse execution or a pending exit.
  Paperbot alerts if force-close is not completing near the end of the
  session; this is not the same as a broker guarantee of closure.

## 5. Corner cases and concrete outcomes

These are code-grounded summaries, not promises about every broker/feed
failure. The recommendation column describes a possible future control, not
functionality currently present.

| Scenario | Current behavior and signal/order effect | Risk and possible improvement (recommendation) |
|---|---|---|
| Gap or breakout immediately reverses | The strategy evaluates completed bars, then submits a limit if the signal and risk checks pass. A later stop may trigger. | A gap through the stop can lose more than planned. Add gap/stress tests and monitor actual fills; no stop-price guarantee is possible. |
| Low-volume breakout / volume disappears | Trend Join requires RVOL >=2 using historical same-time volume; it has no explicit minimum absolute volume. Mean Reversion has no volume threshold beyond VWAP validity. Macro has no volume entry filter. | A relative ratio or VWAP calculation can be misleading in thin markets. Consider an explicit liquidity/volume floor and verify source quality. |
| Price oscillates around an entry threshold | Strategy scans once per bar; the latest completed close must satisfy the condition. A signal/order already working is not resubmitted for the same symbol; fills, cancels and state affect re-entry. | At most one qualifying state per scan, but rapid changes can produce late entries after the signal bar. Consider a signal-age/price-distance bound. |
| Strong move against strategy / volatility spike | Only the configured long entry criteria are checked. A valid signal can occur in a broader selloff; stops and exits manage after entry. | Trend regime and volatility may change faster than these rules. No explicit market-wide regime filter is configured. |
| Market opens far beyond stop/limit | IBKR stop can trigger through its level. Entry limit caps the buy price but can remain unfilled. | Stop loss can be greater than planned. Do not treat stop distance as worst-case loss; add gap-through stress reporting. |
| Stop is touched between evaluations | A broker-held protective stop is intended to remain active between bot passes. Strategy signal/management conditions otherwise use completed bars. | Stop behavior depends on broker/order acceptance, market status, and available liquidity. Check order-status journals and broker state. |
| Halt, suspension, illiquidity, or no valid live quote | Live quote code requires valid non-crossed bid/ask and not-halted status within a short timeout. Entry is skipped without a quote. Exit can fall back to last bar close with an alert. | An exit may be delayed or filled far away after trading resumes. A fallback price is only a reference for a limit, not execution. |
| Out-of-order, duplicate, or invalid historical bars | Backtest data cleaning sorts bars, removes duplicates, drops invalid values, and reports quality warnings. Live market-data code requests completed bars but does not apply the same comprehensive historical dataset validator. | Live signal quality may be degraded by feed corrections or incomplete series. Compare data-quality warnings, and consider live gap/duplicate checks. |
| Missing bars/history or indicator warmup | SMA/RSI/ATR return unavailable until sufficient observations; entry/stop checks fail and skip the symbol. Trend RVOL needs its full 14-day history. Backtest missing data stays missing and is reported, not fabricated. | Fewer trades/data coverage can bias analysis. Require acceptable data completeness before treating a run as informative. |
| NaN/infinity or invalid indicator | Backtest cleaning rejects invalid bars. Live quotes are checked for finite values; many indicator blocks fail if they cannot calculate, and unexpected block exceptions skip the symbol. | Live daily-bar sanitation is less comprehensive than backtest cleaning. Add explicit finite/range checks at the live indicator boundary. |
| Indicator changes after a bar closes | Completed historical bars can be corrected by a provider; the backtest cache tracks changes on refresh. RVOL/VWAP are recalculated from the bars used on each pass. | A later data correction can change a historical signal. Preserve dataset IDs/config snapshots and compare refreshed runs. |
| Conflicting indicators / partial condition set | Entry blocks are an AND-list: first failure rejects the candidate; no weighted score or independent confirmation system is present. | One passing condition cannot override another failing condition. Do not interpret a partial filter funnel as a trade signal. |
| Signal disappears before submission / stale quote | Intraday signal is from last completed bar; order price uses a fresh live quote. It does not re-evaluate the whole signal against every tick at submit time. The entry is GTD and has a finite entry window. | Price can move between evaluation and fill. Add a maximum signal-to-quote delay or deviation guard if desired. |
| Same signal repeats / multiple strategies qualify | Each live process runs one selected strategy. Held symbols and working entries block duplicates; mean reversion allows at most three entries per symbol per day, other profiles default to one. | Running separate bot processes is prevented by the shared process lock; multiple strategies are not automatically portfolio-optimized together. |
| Insufficient buying power / position cap / max orders | Sizing uses buying power and portfolio-value caps; the risk guard checks again before submission. A rejected candidate is logged/blocked. Daily order count and concurrency are enforced. | External account changes between sizing and submission can still create rejection. Keep broker buying power/account state current. |
| Existing/manual position | The strategy manages positions tagged to its own state/order references. Unmanaged positions without Paperbot state are left alone, but they still block an entry in that symbol and count toward the concurrent-position cap; manual open orders count toward the open-order cap. The guard prevents a SELL from exceeding held shares. | A manual position is not necessarily protected by the strategy's stop. Confirm ownership/state before relying on dashboard position counts. |
| Daily loss or drawdown threshold reached | The risk guard blocks new entries after the daily loss threshold; the separate drawdown breaker requires manual reset. Both are shared across strategies. When the halt trips, working entry and exit orders are cancelled once; protective stops stay, and exits are re-sent on later passes. | A halt does not flatten positions. Monitor open risk and broker-held stops. |
| Several exits occur together | Each owned symbol is processed; each exit uses a live quote or fallback bar price. Account buying power is not required to reduce a position. | Many simultaneous exits may compete for liquidity; execution can be partial or delayed. Review order journals and broker open orders. |
| Order rejected or accepted but not filled | Submission and order-status events are journaled. Entries expire after their configured window; exits/stops are reconciled/repriced. A stop that disappears is recreated with an alert where state shows it is owned. | A rejected/missing stop leaves exposure until recovery. Do not infer a fill from "sent"; verify broker order status and position. |
| Partial fill | Broker position/fill state is reconciled. The backtest limits entry fills by a fraction of each bar's volume and models partial fills. Live partials depend on IBKR updates. | Bracket quantity, remaining shares, and stop coverage must be verified. The order manager reconciles protective stop quantity against held shares and working exits. |
| Timeout/unknown submission status or retry | The bot queries/reconciles working orders by its order references before managing; it does not deliberately create unlimited entries for a symbol with an active working BUY. | Network ambiguity is financially sensitive; inspect broker orders before restarting or manually retrying. |
| Exit rejected or session ends with position open | Exit requests are retried/repriced; force-close emits alerts near close. Multi-day positions intentionally remain overnight on GTC stops. Intraday holdings after close are alerted; they are not proven flat by the schedule. | Intraday exposure can persist if the close fails. Verify positions directly in IBKR; never rely only on a generated dashboard. |
| Process restart while pending/open | Working IBKR orders are reread; strategy state is persisted atomically and can re-adopt a position when a Paperbot stop exists. Corrupt state fails closed. | Positions with missing state or stop can become unmanaged and trigger alerts. Reconcile broker positions/orders before restarting or changing strategy. |
| Invalid config or unavailable directory | Profiles validate types, names, required keys, time windows and risk settings before trading. State errors fail closed. Safety-log/directory write failures can prevent normal evaluation. | Correct configuration/storage errors before running; do not bypass failed validation. |
| Stale dashboard or backtest look-ahead | Dashboard files are generated reports and can be stale until rebuilt. The backtester exposes only bars available at simulated time and orders fill on later bars; data quality and execution assumptions are recorded. | Backtests still approximate live quotes/fills and depend on provider history. Check run timestamps, dataset identity, warnings, validation splits, and execution assumptions. |

## 6. Backtesting: what it verifies and what it cannot

The backtester drives the same `rule_strategy.decide()` code, registered
blocks, order manager, sizing, and risk guard used by the live runner. It
replaces the broker and clock with a simulation. It does not place live
orders. A typical on-demand run is:

```powershell
paperbot-backtest run --strategy macro_swing_etf --start 2025-01-01 --end 2025-12-31
```

The current `backtesting/config/backtest.json` sets a **$1,000,000** starting
capital, IBKR as the primary provider and Yahoo Finance as fallback, a
120-calendar-day default period, and 420 calendar days of daily warmup. It
uses 2 bps half-spread, 2 bps additional slippage, 10% of bar volume as the
maximum entry participation per bar, a 1.0 cash buying-power multiplier, and
`stop_first` when a bar could have touched both a stop and an exit. It models
the strategy decision after a three-second delay and fills orders only on
eligible later bars. Commissions are also modeled. The validation settings
split data 60% in-sample, 20% validation, and 20% out-of-sample, use three
walk-forward folds, and mark fewer than 30 trades as insufficient for the
configured assessment threshold. The runner also produces a stress run with
3x the half-spread and slippage (commissions are not multiplied).

These assumptions are explicit in run metadata, but remain approximations:

- The simulated quote is derived from the most recent completed bar plus/minus
  the configured half-spread; it is not historical top-of-book data.
- An order sent during a simulated pass cannot fill earlier than the eligible
  subsequent bar. Bars do not reveal the exact intrabar sequence, so an
  ambiguous stop/exit bar is resolved conservatively as stop-first.
- A real provider's missing session remains missing and is reported as a data
  limitation; it is not filled with invented prices. Bars are sorted,
  deduplicated, validated, and quality-checked. Synthetic data is only for
  demos/tests and is labelled synthetic.
- IBKR is the same data source as live Paperbot, but Yahoo fallback can have
  shorter intraday history (especially for 1-minute data) and potentially
  different corrections/coverage.
- The 1d macro strategy is simulated on a 5-minute tape, using its latest
  completed 5-minute close as the approximation for the live daily quote.

Backtest results are evidence about the tested rules, dates, dataset, and
assumptions only. They do not establish future profitability, live execution
quality, or safety under market conditions absent from that sample.

### False confidence: what a passing backtest does not prove

- A high win rate can conceal a few large losers; inspect net P&L, average win/loss,
  profit factor, drawdown, tail loss, and trade count together.
- Trend Join's gap, breakout, and RVOL checks are related evidence from the same
  move; they are not independent confirmations.
- A one-day backtest is not evidence of statistical reliability. The configured
  assessment expects at least 30 trades; small samples can remain inconclusive.
- Backtest fills are a model. Current assumptions include a 2 bps half-spread,
  2 bps slippage, 10% participation per bar, cash-only buying-power multiplier,
  and conservative `stop_first` for ambiguous bars. Live spreads, market impact,
  fills, halts, and commissions may differ.
- Historical backtests split in-sample, validation, and out-of-sample periods
  and include stress analysis, but those safeguards cannot fix bad or
  unrepresentative source data, overfitting, survivorship bias, or future regime
  changes.
- A synthetic-provider run is demonstration/test data, not market evidence.
  Synthetic runs are labelled in metadata/reports.

## 7. How to verify the implementation

Useful source and test files:

- Strategy configuration: `config/rules.json`; live account mode, order
  policy, and connection settings: `config/config.json`.
- Profile validation and strategy inventory: `src/paperbot/core/profile.py`,
  `src/paperbot/backtesting/strategies.py`.
- Entry blocks: `src/paperbot/blocks/entry.py`; stop blocks:
  `src/paperbot/blocks/stop.py`; management:
  `src/paperbot/blocks/manage.py`; exit blocks:
  `src/paperbot/blocks/exit.py`.
- Decision and signal logging: `src/paperbot/strategies/rule_strategy.py`;
  sizing and broker-order safety: `src/paperbot/core/sizing.py`,
  `src/paperbot/core/risk.py`, `src/paperbot/core/orders.py`, and
  `src/paperbot/core/engine.py`.
- Indicators: `src/paperbot/indicators/`; live bar/quote behavior:
  `src/paperbot/core/market_data.py`.
- Backtest assumptions and data checks: `backtesting/config/backtest.json`,
  `src/paperbot/backtesting/config.py`, `src/paperbot/backtesting/data.py`,
  and `src/paperbot/backtesting/engine.py`.
- Strategy tests: `tests/test_strategy.py`, `tests/test_macro_swing.py`,
  `tests/test_mean_reversion.py`, and `tests/test_risk_policy.py`.
- Backtest tests: `tests/test_backtest_data.py`, `tests/test_backtest_engine.py`,
  and `tests/test_backtest_safety.py`.

Run `paper-bot strategies` to see validated profiles on the installed system.
Run `paperbot-backtest strategies` to list profiles the simulator can discover.
Neither command submits orders.
