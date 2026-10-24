"""Volume indicators."""
import datetime as dt


def relative_volume(history: list[list[tuple[dt.time, float]]], today_volumes: list[float],
                    upto: dt.time, lookback: int) -> float | None:
    """Today's cumulative volume vs. the average over `history` (one list of
    (bar time, volume) per prior day) up to the same time of day. None without
    `lookback` days of history, no bars today, or no average volume."""
    if len(history) < lookback or not today_volumes:
        return None
    avg = sum(sum(v for t, v in day if t <= upto) for day in history) / len(history)
    return sum(today_volumes) / avg if avg > 0 else None


def vwap_bands(bars: list) -> tuple[float, float] | None:
    """Session VWAP of bars (typical price (high + low + close) / 3, volume-weighted)
    and the volume-weighted standard deviation of the typical price around it.
    None without volume."""
    volume = sum(b.volume for b in bars)
    if not bars or volume <= 0:
        return None
    typical = [((b.high + b.low + b.close) / 3, b.volume) for b in bars]
    vwap = sum(p * v for p, v in typical) / volume
    variance = sum(v * (p - vwap) ** 2 for p, v in typical) / volume
    return vwap, variance ** 0.5
