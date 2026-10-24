"""Small deterministic market scenarios for the backtesting tests.

SYNTHETIC TEST DATA ONLY: hand-built bars whose expected fills and P&L can be
worked out by hand (see each test). Never evidence about the real strategy.

Default scenario for one symbol on TRADE_DAY (a Monday), for the Trend Join Long
profile of config/rules.json (5-minute bars):
  * 300 weekdays of daily bars rising from 50 to 100: prior close 100.00,
    prior high 100.50, SMA200 far below (D2 passes)
  * 20 prior sessions of flat 5-minute bars (100.00, volume 1,000 per bar)
  * trade day: premarket bars 09:00-09:25, high 103.50; RTH opens 104.00
    (gap +4%), bars 09:30-09:55 between 103.00 and 105.00 at volume 3,000
    (RVOL 3.0), and the 10:00 bar closes 105.50 (new high of day): the 10:05
    pass sees a passing signal with stop to_tick(103.00 x 0.99) = 101.97
  * from 10:05 on, `after` bars (open, high, low, close[, volume]), then flat at
    the last close until 16:00
"""
import datetime as dt
import json
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import paths
from paperbot.backtesting import config as bt_config
from paperbot.backtesting.data import Bar, InMemoryProvider, MarketData
from paperbot.backtesting.engine import Simulator
from paperbot.core import profile as profiles

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
TRADE_DAY = dt.date(2025, 6, 2)
LIMITS = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}
TJL = "trend_join_long"


def weekdays_before(day: dt.date, n: int) -> list[dt.date]:
    out, d = [], day
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def t(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), ET).astimezone(UTC)


def flat_session(day: dt.date, price: float, volume: float) -> list[Bar]:
    start = dt.datetime.combine(day, dt.time(9, 30), ET)
    return [Bar((start + i * dt.timedelta(minutes=5)).astimezone(UTC), price, price, price, price, volume)
            for i in range(78)]


def trade_day_bars(day: dt.date, scale: float = 1.0, after=(), breakout_index: int = 6,
                   pre_high: float = 103.5) -> list[Bar]:
    s = scale
    bars = [Bar(t(day, 9, m), 103.0 * s, pre_high * s, 102.9 * s, 103.2 * s, 500)
            for m in range(0, 30, 5)]
    start = dt.datetime.combine(day, dt.time(9, 30), ET)
    rth = []
    for i in range(breakout_index):  # quiet opening bars, highs <= 105, low 103.00
        o = 104.0 if i == 0 else 104.2
        rth.append((o, 105.0 if i == 1 else 104.6, 103.0 if i == 2 else 103.8, 104.2, 3000))
    rth.append((104.3, 105.6, 104.2, 105.5, 3000))  # breakout bar: close 105.50 > HOD 105.00
    for row in after:
        rth.append(tuple(row) if len(row) == 5 else (*row, 3000))
    last = rth[-1][3]
    while len(rth) < 78:
        rth.append((last, last, last, last, 3000))
    for i, (o, h, lo, c, v) in enumerate(rth):
        bars.append(Bar((start + i * dt.timedelta(minutes=5)).astimezone(UTC),
                        o * s, h * s, lo * s, c * s, v))
    return bars


def scenario(symbols=("AAA",), after=None, breakout=None, day: dt.date = TRADE_DAY, scale=None):
    """InMemoryProvider for `symbols`; after/breakout/scale are dicts per symbol."""
    after, breakout, scale = after or {}, breakout or {}, scale or {}
    daily, intraday = {}, {}
    history = weekdays_before(day, 300)
    for sym in symbols:
        s = scale.get(sym, 1.0)
        rows = []
        for i, d in enumerate(history):
            c = (50 + 50 * i / (len(history) - 1)) * s
            rows.append(Bar(d, c, c + 0.5 * s, c - 0.5 * s, c, 1e6))
        bars = trade_day_bars(day, s, after.get(sym, ()), breakout.get(sym, 6))
        rth = [b for b in bars if b.date.astimezone(ET).time() >= dt.time(9, 30)]
        rows.append(Bar(day, rth[0].open, max(b.high for b in rth), min(b.low for b in rth),
                        rth[-1].close, sum(b.volume for b in rth)))
        daily[sym] = rows
        intraday[sym] = {d: flat_session(d, 100.0 * s, 1000) for d in history[-20:]}
        intraday[sym][day] = bars
    return InMemoryProvider(daily, intraday)


def rules(key: str = TJL, **risk) -> dict:
    r = json.loads(paths.RULES_FILE.read_text(encoding="utf-8"))
    r["strategies"][key]["risk"].update(risk)
    return r


def profile(key: str = TJL, **risk):
    """The production profile of `key`, with some risk limits changed (a temp copy)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.json"
        path.write_text(json.dumps(rules(key, **risk)), encoding="utf-8")
        return profiles.load(key, path, LIMITS)


def cfg(**over) -> dict:
    c = bt_config.load(paths.PROJECT_ROOT / "does-not-exist.json")
    for k, v in over.items():
        if isinstance(v, dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


def simulate(provider, symbols=("AAA",), prof=None, c=None, start=TRADE_DAY, end=TRADE_DAY, bar="5m"):
    data = MarketData(provider, None, bar=bar)
    data.load_daily(list(symbols), start - dt.timedelta(days=500), end)
    return Simulator(prof or profile(), data, c or cfg(), list(symbols), start, end).run()
