"""Open-position management. These blocks hold settings; the order logic that
applies them lives in core/orders.py (raise_stops, partial_profits), so every
strategy gets the same tested stop and partial-profit machinery."""
from paperbot.blocks.registry import REQUIRED, Block, register
from paperbot.core.risk import ConfigError


@register("manage", "partial_profit")
class PartialProfit(Block):
    """Sell `fraction` of the position (nearest share, always leaving one) once the
    last close reaches entry + at_R x R. Unfilled orders are retried a few times."""
    PARAMS = {"at_R": (float, REQUIRED), "fraction": (float, REQUIRED)}
    NEEDS = "intraday"

    def validate(self, where):
        if not self.fraction < 1:
            raise ConfigError(f"{where}: fraction must be below 1, got {self.fraction!r}")


@register("manage", "breakeven")
class Breakeven(Block):
    """Raise the stop to the entry price once the last close reaches entry + at_R x R."""
    PARAMS = {"at_R": (float, REQUIRED)}
    NEEDS = "intraday"


@register("manage", "trail_swing_low")
class TrailSwingLow(Block):
    """After breakeven, raise the stop to the latest swing low of the strategy's bars:
    a low below the `left` bars before it and no higher than the `right` bars after."""
    PARAMS = {"left": (int, 2), "right": (int, 2)}
    NEEDS = "intraday"
