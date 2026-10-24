"""What a block sees when it checks a symbol (entry) or an open position (exit).

EvalContext loads data lazily and once per evaluation, so blocks never fetch
the same bars twice and the cheapest checks can fail before expensive data is
requested. A block that cannot pass raises Fail(reason).

Timeframes: intraday strategies see today's completed bars of their timeframe
and the last completed close as "price". A 1d strategy sees the completed daily
bars before today and today's LIVE mid price as the stand-in for today's close
(its signals are evaluated in the last minutes of the session).
"""
import datetime as dt

from paperbot.core import market_data as md


# Every key of an entry evaluation result (rule_strategy.evaluate); blocks read
# them freely and may add their own (e.g. vwap, sd).
RESULT_DEFAULTS = {"pass": False, "price": 0.0, "stop": None, "gap": None, "rvol": None}


class Fail(Exception):
    """An entry check failed; str(exception) is the reason logged as "FAIL ..."."""


class EvalContext:
    def __init__(self, ib, contract, symbol: str, now: dt.datetime, profile, result: dict):
        self.ib, self.contract, self.symbol, self.now = ib, contract, symbol, now
        self.profile, self.result = profile, result
        for key, value in RESULT_DEFAULTS.items():
            result.setdefault(key, value)
        self._daily: list | None = None
        self._session: tuple[list, list] | None = None
        self._live: float | None = None

    @property
    def daily_timeframe(self) -> bool:
        return self.profile.timeframe == "1d"

    # ---------------------------------------------------------------- daily
    def daily(self) -> list:
        """Completed daily bars before today; Fail if there are none."""
        if self._daily is None:
            self._daily = md.daily_bars(self.ib, self.contract, self.now.date())
        if not self._daily:
            raise Fail("no daily history")
        return self._daily

    def prior(self):
        """The previous trading day's bar."""
        return self.daily()[-1]

    def closes(self) -> list[float]:
        """Daily closes before today plus today's price (live on 1d, else the last
        completed close): the series daily indicators are computed on."""
        return [b.close for b in self.daily()] + [self.price()]

    # ---------------------------------------------------------------- today
    def session(self) -> tuple[list, list]:
        """Today's completed (premarket, regular-hours) bars of the strategy's
        timeframe. Fail with fewer than 2 regular-hours bars. On first load the
        result's price (last completed close) and gap (vs. the prior close,
        when daily data was loaded) are filled in."""
        if self.daily_timeframe:
            raise Fail("intraday bars are not available to a 1d strategy")
        if self._session is None:
            pre, rth = md.session_bars(self.ib, self.contract, self.now, self.profile.timeframe)
            if len(rth) < 2:
                raise Fail(f"fewer than 2 completed {self.profile.timeframe} bars today")
            self._session = (pre, rth)
            self.result["price"] = float(rth[-1].close)
            if self._daily:
                self.result["gap"] = (rth[0].open / self._daily[-1].close - 1) * 100
        return self._session

    def premarket(self) -> list:
        return self.session()[0]

    def rth(self) -> list:
        return self.session()[1]

    def price(self) -> float:
        if self.daily_timeframe:
            if self._live is None:
                quote = md.quote(self.ib, self.symbol)
                if quote is None:
                    raise Fail("no live quote for today's price")
                self._live = (quote[0] + quote[1]) / 2
                self.result["price"] = self._live
            return self._live
        self.session()
        return self.result["price"]

    def gap(self) -> float:
        """Today's open vs. the prior close, in percent."""
        if self.result["gap"] is None:
            prior_close = self.prior().close
            self.result["gap"] = (self.rth()[0].open / prior_close - 1) * 100
        return self.result["gap"]


class ExitContext:
    """One open position, for exit blocks. `st` is its strategy state; `opened` the
    ET date it was entered."""

    def __init__(self, ib, profile, symbol: str, position, st: dict, opened: dt.date | None,
                 now: dt.datetime):
        self.ib, self.profile, self.symbol, self.position = ib, profile, symbol, position
        self.st, self.opened, self.now = st, opened, now
        self._daily: list | None = None
        self._price: float | None = None
        self._priced = False

    def daily(self) -> list:
        if self._daily is None:
            c = md.contract(self.ib, self.symbol)
            self._daily = md.daily_bars(self.ib, c, self.now.date()) if c else []
        return self._daily

    def price(self) -> float | None:
        """Live mid price, or None without a valid quote (the exit waits)."""
        if not self._priced:
            quote = md.quote(self.ib, self.symbol)
            self._price = (quote[0] + quote[1]) / 2 if quote else None
            self._priced = True
        return self._price

    def rth(self) -> list:
        """Today's completed regular-hours bars of the strategy's (intraday) timeframe."""
        if not hasattr(self, "_rth"):
            self._rth = md.rth_bars(self.ib, self.symbol, self.now, self.profile.timeframe)
        return self._rth

    def held_days(self) -> int | None:
        """Trading days since the entry day (0 on the entry day), counted from the
        daily bars, so weekends and market holidays are not counted."""
        if self.opened is None:
            return None
        sessions = sum(1 for b in self.daily() if b.date > self.opened)
        return sessions + (1 if self.now.date() > self.opened else 0)

    def held_minutes(self) -> float | None:
        opened_at = self.st.get("opened_at")
        if not opened_at:
            return None
        return (self.now - dt.datetime.fromisoformat(opened_at)).total_seconds() / 60
