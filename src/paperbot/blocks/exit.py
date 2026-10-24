"""Position exits.

force_close is applied by core/orders.force_close at time_filter.force_close_et.
Every other exit is a signal: check(ctx) returns the reason to close the
position now, or None. core/orders.signal_exits then sends a marketable SELL in
the protective stop's OCA group (the stop stays live until the SELL fills) and
re-prices it until the position is flat; a decided exit is kept across days.
Multi-day strategies never exit a position on the day it was entered.
"""
import datetime as dt

from paperbot.blocks.context import ExitContext
from paperbot.blocks.registry import REQUIRED, Block, register
from paperbot.core.common import hhmm
from paperbot.core.risk import ConfigError
from paperbot.indicators import sma, vwap_bands


class ExitBlock(Block):
    SIGNAL = True

    def check(self, ctx: ExitContext) -> str | None:
        raise NotImplementedError


def _valid_time(where: str, name: str, value):
    if value is None:
        return
    try:
        hhmm(value)
    except (ValueError, AttributeError):
        raise ConfigError(f'{where}: {name} must be "HH:MM" (ET), got {value!r}') from None


def _too_early(at_et: str | None, now: dt.datetime) -> bool:
    return at_et is not None and now.time() < hhmm(at_et)


@register("exit", "force_close")
class ForceClose(Block):
    """Flatten every position from time_filter.force_close_et until the close, with a
    marketable limit SELL in the protective stop's OCA group, re-priced each pass."""
    SIGNAL = False
    NEEDS = "intraday"


@register("exit", "close_above_sma")
class CloseAboveSmaExit(ExitBlock):
    """Exit when today's live price is above the SMA(period) of daily closes including
    today's (from at_et on, e.g. 15:50 for a close-based rule)."""
    PARAMS = {"period": (int, REQUIRED), "at_et": (str, None)}

    def validate(self, where):
        _valid_time(where, "at_et", self.at_et)

    def check(self, ctx):
        if _too_early(self.at_et, ctx.now):
            return None
        price = ctx.price()
        if price is None:
            return None
        average = sma([b.close for b in ctx.daily()] + [price], self.period)
        if average is not None and price > average:
            return f"price {price:.2f} above SMA{self.period} {average:.2f}"
        return None


@register("exit", "time_stop")
class TimeStop(ExitBlock):
    """Exit after `days` trading days held (multi-day) or `minutes` (intraday), from
    at_et on if given. Exactly one of days / minutes."""
    PARAMS = {"days": (int, None), "minutes": (int, None), "at_et": (str, None)}

    def validate(self, where):
        if (self.days is None) == (self.minutes is None):
            raise ConfigError(f"{where}: time_stop needs exactly one of days or minutes")
        _valid_time(where, "at_et", self.at_et)

    def check(self, ctx):
        if _too_early(self.at_et, ctx.now):
            return None
        if self.days is not None:
            held = ctx.held_days()
            if held is not None and held >= self.days:
                return f"held {held} trading days (time stop {self.days})"
            return None
        held = ctx.held_minutes()
        if held is not None and held >= self.minutes:
            return f"held {held:.0f} min (time stop {self.minutes})"
        return None


@register("exit", "target_vwap")
class TargetVwap(ExitBlock):
    """Exit when the live price is back at or above today's VWAP (the mean)."""
    NEEDS = "intraday"

    def check(self, ctx):
        price, bands = ctx.price(), vwap_bands(ctx.rth())
        if price is None or bands is None:
            return None
        if price >= bands[0]:
            return f"price {price:.2f} back at VWAP {bands[0]:.2f}"
        return None
