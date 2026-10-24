"""Initial protective stop rules. compute() returns the stop price for an entry
that passed every entry check (the context already holds today's bars)."""
from paperbot.blocks.context import EvalContext, Fail
from paperbot.blocks.registry import REQUIRED, Block, register
from paperbot.core.sizing import to_tick
from paperbot.indicators import atr, vwap_bands


class StopBlock(Block):
    def compute(self, ctx: EvalContext) -> float:
        raise NotImplementedError


@register("stop", "low_of_day_minus_pct")
class LowOfDayMinusPct(StopBlock):
    """pct percent below today's regular-hours low."""
    PARAMS = {"pct": (float, REQUIRED)}
    NEEDS = "intraday"

    def compute(self, ctx):
        low = min(b.low for b in ctx.rth())
        return to_tick(low * (1 - self.pct / 100), "STOP")


@register("stop", "atr_multiple")
class AtrMultiple(StopBlock):
    """`multiple` x the Wilder ATR(period) of daily bars below today's price."""
    PARAMS = {"period": (int, 14), "multiple": (float, REQUIRED)}

    def compute(self, ctx):
        value = atr(ctx.daily(), self.period)
        if value is None:
            raise Fail(f"ATR({self.period}) n/a: fewer than {self.period + 1} daily bars")
        return to_tick(ctx.price() - self.multiple * value, "STOP")


@register("stop", "sigma_below_entry")
class SigmaBelowEntry(StopBlock):
    """`sigma` x today's VWAP standard deviation below the last completed close."""
    PARAMS = {"sigma": (float, REQUIRED)}
    NEEDS = "intraday"

    def compute(self, ctx):
        bands = vwap_bands(ctx.rth())
        if bands is None or bands[1] <= 0:
            raise Fail("VWAP deviation n/a: no stop distance")
        return to_tick(ctx.price() - self.sigma * bands[1], "STOP")
