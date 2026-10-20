"""Momentum indicators."""


def rsi(values: list[float], period: int) -> float | None:
    """Wilder's Relative Strength Index of the last value, 0-100. Seeded with the
    simple average of the first `period` changes, then Wilder-smoothed over the
    rest of the series (a longer series gives the standard, stable value).
    None with fewer than period + 1 values."""
    if len(values) < period + 1:
        return None
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(c, 0.0) for c in changes[:period]) / period
    loss = sum(max(-c, 0.0) for c in changes[:period]) / period
    for c in changes[period:]:
        gain = (gain * (period - 1) + max(c, 0.0)) / period
        loss = (loss * (period - 1) + max(-c, 0.0)) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)
