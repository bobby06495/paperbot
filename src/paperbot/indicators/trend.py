"""Trend indicators."""


def sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` values; None with fewer values."""
    return sum(values[-period:]) / period if len(values) >= period else None


def swing_low(bars: list, left: int, right: int) -> float | None:
    """Most recent pivot low: lower than `left` bars before it and no higher
    than `right` bars after it."""
    for i in range(len(bars) - right - 1, left - 1, -1):
        low = bars[i].low
        if (all(low < b.low for b in bars[i - left:i])
                and all(low <= b.low for b in bars[i + 1:i + right + 1])):
            return low
    return None
