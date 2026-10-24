"""Performance and risk metrics, computed only from a run's ledgers.

Inputs are the rows the simulator wrote (trades, equity, daily), never numbers
made up for display. A metric that cannot be computed is None, and
`na_reasons[key]` says why ("N/A - no losing trades", ...). Money is USD.

Definitions (also shown on the dashboard, DEFINITIONS below):
  * Trade P&L is NET: after commissions, with slippage already in the fill prices.
    A position sold in pieces (partial profit, then the rest) is ONE trade.
  * Percentages say their basis: total return vs starting capital, win rate vs
    completed trades, drawdown vs the previous equity peak, exposure vs equity.
  * Open positions at the end of the test are marked at their last 5-minute close
    and counted in unrealized P&L and ending equity, not in completed trades.
"""
import datetime as dt
import math
import random
import statistics

DEFINITIONS = {
    "starting_capital": "Cash the simulation started with (USD).",
    "ending_equity": "Cash plus open positions valued at their last price, at the end of the test (USD).",
    "net_pnl": "Ending equity minus starting capital: the overall result after all costs (USD).",
    "total_return_pct": "Net P&L as a percentage of starting capital.",
    "realized_pnl": "Profit or loss locked in by sales during the test, after commissions (USD).",
    "unrealized_pnl": "Paper profit or loss on positions still open at the end, at their last price (USD).",
    "gross_profits": "Sum of the net results of all winning completed trades (USD).",
    "gross_losses": "Sum of the net results of all losing completed trades (USD, shown negative).",
    "completed_trades": "Round trips that were fully closed during the test. A position sold in pieces counts once.",
    "open_positions_end": "Positions still held when the test ended.",
    "winning_trades": "Completed trades with a net result above zero.",
    "losing_trades": "Completed trades with a net result below zero.",
    "breakeven_trades": "Completed trades with a net result of exactly zero (to the cent).",
    "win_rate_pct": "Winning trades as a percentage of completed trades.",
    "avg_win": "Average net result of the winning trades (USD).",
    "avg_loss": "Average net result of the losing trades (USD).",
    "largest_win": "Best single completed trade (USD).",
    "largest_loss": "Worst single completed trade (USD).",
    "profit_factor": "Gross profits divided by the absolute value of gross losses. Above 1 means winners outweighed losers.",
    "expectancy": "Average net result per completed trade (USD).",
    "avg_r": "Average result per trade in R, where 1R is the money that would have been lost at the initial stop.",
    "max_drawdown_pct": "Largest percentage fall in equity from a previous peak, measured every 5 minutes.",
    "max_drawdown_usd": "The same largest fall, in dollars.",
    "avg_holding_minutes": "Average time between the first entry fill and the final exit (minutes).",
    "total_commission": "All simulated commissions (IBKR Fixed pricing model, USD).",
    "total_slippage": "Estimated cost of spread and slippage, already included in the fill prices (USD).",
    "cost_share_of_gross_pct": "Commissions plus slippage as a percentage of the absolute gross (pre-cost) P&L.",
    "avg_exposure_pct": "Average value of open positions as a percentage of equity, across all 5-minute marks.",
    "max_exposure_pct": "Highest value of open positions as a percentage of equity.",
    "time_in_market_pct": "Share of 5-minute marks with at least one open position.",
    "max_concurrent_positions": "Most positions held at the same time.",
    "max_consecutive_losses": "Longest run of losing completed trades in a row.",
    "top3_share_of_net_pct": "Net P&L of the three best trades as a percentage of total net P&L (shows dependence on a few trades).",
    "daily_volatility_pct": "Standard deviation of daily returns (percent of the day's starting equity).",
    "sharpe_like": "Average daily return divided by its standard deviation, times the square root of 252. A rough risk-adjusted figure; not reliable on short samples.",
    "benchmark_return_pct": "Buy-and-hold return of the benchmark ETF over the same days (first open to last close).",
    "mean_trade_ci95": "Range that would contain the average trade result in 95% of bootstrap resamples of the trades. If it includes 0, the average could plausibly be zero.",
    "days_with_trades": "Trading days on which at least one trade was completed.",
    "days_without_trades": "Trading days with data but no completed trade.",
}


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def drawdown(values: list[float]) -> tuple[float | None, float | None, int | None, int | None]:
    """(max drawdown %, max drawdown USD, peak index, trough index) of an equity series."""
    if not values:
        return None, None, None, None
    peak, peak_i = values[0], 0
    best = (0.0, 0.0, 0, 0)
    for i, v in enumerate(values):
        if v > peak:
            peak, peak_i = v, i
        dd_usd = peak - v
        dd_pct = 100 * dd_usd / peak if peak > 0 else 0.0
        if dd_pct > best[0]:
            best = (dd_pct, dd_usd, peak_i, i)
    return round(best[0], 4), round(best[1], 2), best[2], best[3]


def bootstrap_mean_ci(values: list[float], samples: int, seed: int) -> tuple[float, float] | None:
    if len(values) < 5:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(samples))
    return round(means[int(0.025 * samples)], 2), round(means[int(0.975 * samples) - 1], 2)


def compute(trades: list[dict], open_trades: list[dict], equity: list[dict], daily: list[dict],
            capital: float, benchmark: list | None = None, validation: dict | None = None) -> dict:
    validation = validation or {}
    na: dict[str, str] = {}
    m: dict = {"currency": "USD", "starting_capital": round(capital, 2)}
    nets = [_num(t, "net_pnl") or 0.0 for t in trades]
    gross = [_num(t, "gross_pnl") or 0.0 for t in trades]
    ending = equity[-1]["equity"] if equity else capital
    m["ending_equity"] = round(ending, 2)
    m["net_pnl"] = round(ending - capital, 2)
    m["total_return_pct"] = round(100 * (ending - capital) / capital, 4)
    unrealized = sum(_num(t, "unrealized_pnl") or 0.0 for t in open_trades)
    m["unrealized_pnl"] = round(unrealized, 2)
    m["realized_pnl"] = round(ending - capital - unrealized, 2)
    m["completed_trades"] = len(trades)
    m["open_positions_end"] = len(open_trades)
    wins = [n for n in nets if n > 0.005]
    losses = [n for n in nets if n < -0.005]
    m["winning_trades"], m["losing_trades"] = len(wins), len(losses)
    m["breakeven_trades"] = len(nets) - len(wins) - len(losses)
    m["gross_profits"] = round(sum(wins), 2)
    m["gross_losses"] = round(sum(losses), 2)

    def put(key, value, reason):
        m[key] = value
        if value is None:
            na[key] = reason

    no_trades = "N/A - no completed trades"
    put("win_rate_pct", round(100 * len(wins) / len(nets), 2) if nets else None, no_trades)
    put("avg_win", round(statistics.fmean(wins), 2) if wins else None, "N/A - no winning trades")
    put("avg_loss", round(statistics.fmean(losses), 2) if losses else None, "N/A - no losing trades")
    put("largest_win", round(max(wins), 2) if wins else None, "N/A - no winning trades")
    put("largest_loss", round(min(losses), 2) if losses else None, "N/A - no losing trades")
    put("profit_factor", round(sum(wins) / abs(sum(losses)), 3) if losses else None,
        "N/A - no losing trades (division by zero)" if nets else no_trades)
    put("expectancy", round(statistics.fmean(nets), 2) if nets else None, no_trades)
    rs = [_num(t, "r_multiple") for t in trades]
    rs = [r for r in rs if r is not None]
    put("avg_r", round(statistics.fmean(rs), 3) if rs else None, no_trades)
    holds = [_num(t, "holding_minutes") for t in trades]
    holds = [h for h in holds if h is not None]
    put("avg_holding_minutes", round(statistics.fmean(holds), 1) if holds else None, no_trades)

    values = [r["equity"] for r in equity]
    dd_pct, dd_usd, peak_i, trough_i = drawdown(values)
    put("max_drawdown_pct", dd_pct, "N/A - no equity data")
    put("max_drawdown_usd", dd_usd, "N/A - no equity data")
    m["max_drawdown_peak_time"] = equity[peak_i]["time_et"] if peak_i is not None and equity else None
    m["max_drawdown_trough_time"] = equity[trough_i]["time_et"] if trough_i is not None and equity else None

    all_trades = trades + open_trades
    m["total_commission"] = round(sum(_num(t, "commission") or 0 for t in all_trades), 2)
    m["total_slippage"] = round(sum(_num(t, "slippage_cost") or 0 for t in all_trades), 2)
    gross_abs = abs(sum(gross))
    put("cost_share_of_gross_pct",
        round(100 * (m["total_commission"] + m["total_slippage"]) / gross_abs, 2) if gross_abs > 0 else None,
        no_trades if not trades else "N/A - gross P&L is zero")

    exposures = [r["exposure_pct"] for r in equity if r.get("exposure_pct") is not None]
    put("avg_exposure_pct", round(statistics.fmean(exposures), 3) if exposures else None, "N/A - no equity data")
    put("max_exposure_pct", round(max(exposures), 3) if exposures else None, "N/A - no equity data")
    put("time_in_market_pct",
        round(100 * sum(1 for r in equity if r["open_positions"]) / len(equity), 2) if equity else None,
        "N/A - no equity data")
    m["max_concurrent_positions"] = max((r["open_positions"] for r in equity), default=0)

    streak = best = 0
    for n in nets:
        streak = streak + 1 if n < -0.005 else 0
        best = max(best, streak)
    m["max_consecutive_losses"] = best
    total_net = sum(nets)
    top3 = sum(sorted(nets, reverse=True)[:3])
    put("top3_share_of_net_pct", round(100 * top3 / total_net, 1) if total_net > 0 and len(nets) >= 3 else None,
        "N/A - total net P&L is not positive or fewer than 3 trades")

    rets = [r["pnl"] / r["start_equity"] * 100 for r in daily if r["start_equity"] > 0]
    if len(rets) >= 20 and statistics.pstdev(rets) > 0:
        sd = statistics.stdev(rets)
        m["daily_volatility_pct"] = round(sd, 4)
        m["sharpe_like"] = round(statistics.fmean(rets) / sd * math.sqrt(252), 3)
    else:
        m["daily_volatility_pct"] = m["sharpe_like"] = None
        na["daily_volatility_pct"] = na["sharpe_like"] = "N/A - fewer than 20 trading days or no variation"

    if benchmark and len(benchmark) >= 2:
        m["benchmark_return_pct"] = round(100 * (benchmark[-1].close / benchmark[0].open - 1), 3)
    else:
        m["benchmark_return_pct"] = None
        na["benchmark_return_pct"] = "N/A - no benchmark data for the period"

    ci = bootstrap_mean_ci(nets, validation.get("bootstrap_samples", 2000),
                           validation.get("bootstrap_seed", 7))
    put("mean_trade_ci95", list(ci) if ci else None, "N/A - fewer than 5 completed trades")
    m["days"] = len(daily)
    m["days_with_trades"] = sum(1 for r in daily if r["trades_closed"])
    m["days_without_trades"] = m["days"] - m["days_with_trades"]
    m["na_reasons"] = na
    return m


def monthly(daily: list[dict]) -> list[dict]:
    """Per calendar month: P&L, return vs the month's starting equity, trades, costs,
    and the deepest drawdown of daily closing equity within the month."""
    months: dict[str, list] = {}
    for r in daily:
        months.setdefault(r["date"][:7], []).append(r)
    out = []
    for key in sorted(months):
        rows = months[key]
        start = rows[0]["start_equity"]
        closes = [start] + [r["end_equity"] for r in rows]
        dd_pct, _, _, _ = drawdown(closes)
        pnl = sum(r["pnl"] for r in rows)
        out.append({"month": key, "pnl": round(pnl, 2),
                    "return_pct": round(100 * pnl / start, 3) if start > 0 else None,
                    "trades": sum(r["trades_closed"] for r in rows),
                    "wins": sum(r["wins"] for r in rows), "losses": sum(r["losses"] for r in rows),
                    "commission": round(sum(r["commission"] for r in rows), 2),
                    "trading_days": len(rows),
                    "days_without_trades": sum(1 for r in rows if not r["trades_closed"]),
                    "max_drawdown_pct": dd_pct})
    return out


def breakdown(trades: list[dict], key: str) -> list[dict]:
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(str(t.get(key) or "n/a"), []).append(_num(t, "net_pnl") or 0.0)
    rows = [{"key": k, "trades": len(v), "net_pnl": round(sum(v), 2),
             "win_rate_pct": round(100 * sum(1 for x in v if x > 0.005) / len(v), 1)}
            for k, v in groups.items()]
    return sorted(rows, key=lambda r: -abs(r["net_pnl"]))


# ---------------------------------------------------------------- validation periods

SEGMENTS = ("in_sample", "validation", "out_of_sample")


def split_days(days: list[str], fractions: dict) -> dict[str, list[str]]:
    """Chronological split: earliest days in-sample, then validation, then out-of-sample."""
    n = len(days)
    a = round(n * fractions["in_sample"])
    b = a + round(n * fractions["validation"])
    return {"in_sample": days[:a], "validation": days[a:b], "out_of_sample": days[b:]}


def segment_metrics(name: str, seg_days: list[str], trades: list[dict], equity: list[dict],
                    daily: list[dict]) -> dict:
    """Metrics for trades EXITED within the segment's days and its equity marks.
    Return basis: equity at the start of the segment."""
    if not seg_days:
        return {"segment": name, "days": 0, "start": None, "end": None, "status": "empty"}
    first, last = seg_days[0], seg_days[-1]
    in_seg = [t for t in trades if first <= str(t.get("exit_time_et", ""))[:10] <= last]
    rows = [r for r in daily if first <= r["date"] <= last]
    eq = [r["equity"] for r in equity if first <= r["time_et"][:10] <= last]
    nets = [_num(t, "net_pnl") or 0.0 for t in in_seg]
    wins = [n for n in nets if n > 0.005]
    losses = [n for n in nets if n < -0.005]
    start_eq = rows[0]["start_equity"] if rows else None
    pnl = sum(r["pnl"] for r in rows)
    dd_pct, _, _, _ = drawdown(([start_eq] if start_eq else []) + eq)
    return {"segment": name, "days": len(seg_days), "start": first, "end": last,
            "net_pnl": round(pnl, 2),
            "return_pct": round(100 * pnl / start_eq, 4) if start_eq else None,
            "trades": len(nets),
            "win_rate_pct": round(100 * len(wins) / len(nets), 2) if nets else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
            "expectancy": round(statistics.fmean(nets), 2) if nets else None,
            "max_drawdown_pct": dd_pct,
            "commission": round(sum(r["commission"] for r in rows), 2),
            "status": "ok"}


def periods(days: list[str], trades: list[dict], equity: list[dict], daily: list[dict],
            validation: dict) -> dict:
    split = split_days(days, validation)
    segments = {k: segment_metrics(k, v, trades, equity, daily) for k, v in split.items()}
    folds = []
    k = int(validation.get("walk_forward_folds") or 0)
    if k >= 2 and len(days) >= k * 5:
        size = len(days) // k
        for i in range(k):
            chunk = days[i * size: (i + 1) * size if i < k - 1 else len(days)]
            folds.append(segment_metrics(f"fold_{i + 1}", chunk, trades, equity, daily))
    return {"segments": segments, "folds": folds,
            "method": ("Chronological split of the trading days: the first "
                       f"{validation['in_sample']:.0%} in-sample, the next {validation['validation']:.0%} "
                       f"validation, the last {validation['out_of_sample']:.0%} out-of-sample. Trades are "
                       "assigned to the segment in which they exited. Walk-forward folds are consecutive "
                       "equal blocks used to check whether results are consistent over time.")}


def iso_week(day: str) -> str:
    y, w, _ = dt.date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"
