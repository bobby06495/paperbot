"""Entry checks. Each returns the reason it passed (or None to log nothing) and
raises Fail(reason) when the symbol does not qualify. Messages start with the
block's optional label, e.g. "D3 gap 4.1%"."""
from paperbot.blocks.context import EvalContext, Fail
from paperbot.blocks.registry import REQUIRED, Block, register
from paperbot.core import market_data as md
from paperbot.indicators import relative_volume, rsi, sma, vwap_bands


class EntryBlock(Block):
    def check(self, ctx: EvalContext) -> str | None:
        raise NotImplementedError


# ---------------------------------------------------------------- price

@register("entry", "min_prior_close")
class MinPriorClose(EntryBlock):
    """The previous day's close is at least min_usd."""
    PARAMS = {"min_usd": (float, REQUIRED)}

    def check(self, ctx):
        close = ctx.prior().close
        if close < self.min_usd:
            raise Fail(f"{self.prefix}prior close {close:.2f} < min {self.min_usd}")
        return None


@register("entry", "min_price")
class MinPrice(EntryBlock):
    """The last completed close today is at least min_usd."""
    PARAMS = {"min_usd": (float, REQUIRED)}

    def check(self, ctx):
        price = ctx.price()
        if price < self.min_usd:
            raise Fail(f"{self.prefix}price {price:.2f} < min {self.min_usd}")
        return f"{self.prefix}price {price:.2f} >= {self.min_usd}"


# ---------------------------------------------------------------- daily

@register("entry", "prior_close_above_sma")
class PriorCloseAboveSma(EntryBlock):
    """The previous close is above the simple moving average of daily closes."""
    PARAMS = {"period": (int, 200)}

    def check(self, ctx):
        close = ctx.prior().close
        average = sma([b.close for b in ctx.daily()], self.period)
        if not (average and close > average):
            shown = f"{average:.2f}" if average else f"n/a (<{self.period} days)"
            raise Fail(f"{self.prefix}prior close {close:.2f} not above SMA{self.period} {shown}")
        return f"{self.prefix}prior close {close:.2f} > SMA{self.period} {average:.2f}"


@register("entry", "above_prior_day_high")
class AbovePriorDayHigh(EntryBlock):
    """The last completed close is above the previous day's high."""

    def check(self, ctx):
        high = ctx.prior().high
        if ctx.price() <= high:
            raise Fail(f"{self.prefix}price not above prior-day high {high:.2f}")
        return f"{self.prefix}above prior-day high {high:.2f}"


# ---------------------------------------------------------------- intraday

@register("entry", "min_gap_pct")
class MinGapPct(EntryBlock):
    """Today's open gapped up at least min_pct percent from the previous close."""
    PARAMS = {"min_pct": (float, REQUIRED)}
    NEEDS = "intraday"

    def check(self, ctx):
        gap = ctx.gap()
        if gap < self.min_pct:
            raise Fail(f"{self.prefix}gap {gap:.1f}% < {self.min_pct}%")
        return f"{self.prefix}gap {gap:.1f}%"


@register("entry", "above_premarket_high")
class AbovePremarketHigh(EntryBlock):
    """The last completed close is above today's premarket high."""
    NEEDS = "intraday"

    def check(self, ctx):
        pre = ctx.premarket()
        if not pre:
            raise Fail(f"{self.prefix}no premarket bars")
        high = max(b.high for b in pre)
        if ctx.price() <= high:
            raise Fail(f"{self.prefix}price not above premarket high {high:.2f}")
        return f"{self.prefix}above premarket high {high:.2f}"


@register("entry", "new_high_of_day")
class NewHighOfDay(EntryBlock):
    """The last completed close is above every earlier regular-hours high today."""
    NEEDS = "intraday"

    def check(self, ctx):
        high = max(b.high for b in ctx.rth()[:-1])
        if ctx.price() <= high:
            raise Fail(f"{self.prefix}price not above today's high {high:.2f}")
        return f"{self.prefix}above today's high {high:.2f}"


@register("entry", "min_rvol")
class MinRvol(EntryBlock):
    """Relative volume: today's volume so far vs. the average of the last
    lookback_days at the same time of day is at least min."""
    PARAMS = {"min": (float, REQUIRED), "lookback_days": (int, 14)}
    NEEDS = "intraday"

    def check(self, ctx):
        rth = ctx.rth()
        history = md.volume_history(ctx.ib, ctx.contract, ctx.now.date(), self.lookback_days,
                                    ctx.profile.timeframe)
        rvol = relative_volume(history, [b.volume for b in rth], md.et(rth[-1]).time(),
                               self.lookback_days)
        ctx.result["rvol"] = rvol
        if rvol is None or rvol < self.min:
            shown = f"{rvol:.2f}" if rvol is not None else "n/a"
            raise Fail(f"{self.prefix}RVOL {shown} < {self.min}")
        return f"{self.prefix}RVOL {rvol:.2f}"


# ---------------------------------------------------------------- daily trend / pullback

@register("entry", "close_above_sma")
class CloseAboveSma(EntryBlock):
    """Today's price (1d: the live price; intraday: the last completed close) is
    above the simple moving average of daily closes including today's."""
    PARAMS = {"period": (int, REQUIRED)}

    def check(self, ctx):
        closes = ctx.closes()
        average, price = sma(closes, self.period), closes[-1]
        if average is None:
            raise Fail(f"{self.prefix}SMA{self.period} n/a (<{self.period} days)")
        if price <= average:
            raise Fail(f"{self.prefix}price {price:.2f} not above SMA{self.period} {average:.2f}")
        return f"{self.prefix}price {price:.2f} > SMA{self.period} {average:.2f}"


@register("entry", "rsi_below")
class RsiBelow(EntryBlock):
    """Wilder RSI of daily closes including today's is below max (a pullback)."""
    PARAMS = {"period": (int, 2), "max": (float, REQUIRED)}

    def check(self, ctx):
        value = rsi(ctx.closes(), self.period)
        if value is None:
            raise Fail(f"{self.prefix}RSI({self.period}) n/a")
        if value >= self.max:
            raise Fail(f"{self.prefix}RSI({self.period}) {value:.1f} not below {self.max}")
        return f"{self.prefix}RSI({self.period}) {value:.1f} < {self.max}"


# ---------------------------------------------------------------- mean reversion

@register("entry", "below_vwap_sigma")
class BelowVwapSigma(EntryBlock):
    """The last completed close is at or below today's VWAP minus `sigma` volume-weighted
    standard deviations (a stretch below fair value). Stores vwap/sd in the result."""
    PARAMS = {"sigma": (float, REQUIRED)}
    NEEDS = "intraday"

    def check(self, ctx):
        bands = vwap_bands(ctx.rth())
        if bands is None or bands[1] <= 0:
            raise Fail(f"{self.prefix}VWAP n/a (no volume or no spread yet)")
        vwap, sd = bands
        ctx.result["vwap"], ctx.result["sd"] = round(vwap, 4), round(sd, 4)
        price, line = ctx.price(), vwap - self.sigma * sd
        if price > line:
            raise Fail(f"{self.prefix}price {price:.2f} above VWAP {vwap:.2f} - {self.sigma}sd ({line:.2f})")
        return f"{self.prefix}price {price:.2f} <= VWAP {vwap:.2f} - {self.sigma}sd ({line:.2f})"
