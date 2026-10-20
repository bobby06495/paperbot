"""Volatility indicators."""


def atr(bars: list, period: int) -> float | None:
    """Wilder's Average True Range of bars with high/low/close. True range =
    max(high - low, |high - previous close|, |low - previous close|). Seeded with
    the average of the first `period` true ranges, then Wilder-smoothed. None with
    fewer than period + 1 bars."""
    if len(bars) < period + 1:
        return None
    ranges = [max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close))
              for prev, b in zip(bars, bars[1:])]
    value = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value
