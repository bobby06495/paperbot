"""Historical market data for backtests: providers, an on-disk cache and validation.

Providers return Bar objects. Intraday bars ("tape") come in one bar size per run
(1m, 5m, 15m or 1h, from 04:00 to 16:00 ET including the premarket); daily bars
are regular-hours daily bars.

  IBKRProvider       the same source the live bot uses (reqHistoricalData, TRADES).
                     Read-only API connection with its own client id; every order
                     method of the connection object is replaced by one that raises.
                     Needs IB Gateway logged in to the PAPER account.
  YFinanceProvider   Yahoo Finance. No Gateway needed, but short intraday history:
                     1m ~30 days, 5m/15m ~60 days, 1h ~2 years. Older days come back
                     empty and are reported as a data limitation.
  SyntheticProvider  generated prices for tests and demos ONLY. Every run that uses
                     it is labelled SYNTHETIC in its metadata and every report.
  InMemoryProvider   hand-built bars for unit tests.

Nothing here invents prices for a real provider: a day the provider has no data
for stays missing and is reported (DataQuality), never filled in.

DataCache stores what a real provider returned under backtesting/data_cache/
(git-ignored), one CSV per symbol-day and bar size and one per symbol for daily
bars, with a content hash so a run can record exactly which data it used and a
refetch can detect that the provider corrected its history (changes.jsonl).
"""
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
PRE_OPEN = dt.time(4, 0)
RTH_OPEN, RTH_CLOSE = dt.time(9, 30), dt.time(16, 0)
BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
IBKR_SIZES = {"1m": "1 min", "5m": "5 mins", "15m": "15 mins", "1h": "1 hour"}
YAHOO_DAYS = {"1m": 29, "5m": 59, "15m": 59, "1h": 720}  # how far back Yahoo serves each size
YAHOO_CHUNK = {"1m": 7, "5m": 59, "15m": 59, "1h": 720}  # max days per Yahoo request

log = logging.getLogger("paperbot.backtest")


class DataError(RuntimeError):
    """Historical data is missing or unusable; the backtest stops with this message."""


class SymbolNotFound(LookupError):
    """The provider does not know one symbol (delisted, renamed): reported, not fatal."""


@dataclass(frozen=True, slots=True)
class Bar:
    date: object  # intraday: aware datetime (bar start); daily: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def today_et() -> dt.date:
    return dt.datetime.now(ET).date()


def rth_bar_count(bar: str) -> int:
    return 390 // BAR_MINUTES[bar]


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------- validation

@dataclass
class DataQuality:
    """Everything a report needs to say about the data a run used."""
    provider: str
    synthetic: bool = False
    bar: str = "5m"
    requested_start: str | None = None
    requested_end: str | None = None
    actual_start: str | None = None
    actual_end: str | None = None
    symbols_requested: list = field(default_factory=list)
    symbols_with_data: list = field(default_factory=list)
    symbols_without_data: list = field(default_factory=list)
    trading_days: int = 0
    intraday_days_loaded: int = 0
    intraday_days_missing: list = field(default_factory=list)  # [symbol, day]
    duplicate_bars: int = 0
    unordered_series: int = 0
    invalid_bars: int = 0
    off_grid_bars: int = 0
    incomplete_sessions: list = field(default_factory=list)  # [symbol, day, bars]
    warnings: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def warn(self, text: str):
        if text not in self.warnings:
            self.warnings.append(text)

    def limit(self, text: str):
        if text not in self.limitations:
            self.limitations.append(text)

    @property
    def status(self) -> str:
        if self.errors:
            return "fail"
        if (self.warnings or self.invalid_bars or self.duplicate_bars or self.unordered_series
                or self.intraday_days_missing or self.incomplete_sessions or self.synthetic):
            return "warn"
        return "pass"

    def to_dict(self) -> dict:
        out = dict(self.__dict__)
        out["status"] = self.status
        out["incomplete_sessions"] = self.incomplete_sessions[:500]
        out["intraday_days_missing"] = self.intraday_days_missing[:500]
        out["incomplete_sessions_total"] = len(self.incomplete_sessions)
        out["intraday_days_missing_total"] = len(self.intraday_days_missing)
        return out


def _valid_prices(b: Bar) -> bool:
    values = (b.open, b.high, b.low, b.close)
    if any(v is None or not math.isfinite(v) or v <= 0 for v in values):
        return False
    if b.volume is None or not math.isfinite(b.volume) or b.volume < 0:
        return False
    tol = 1e-6 * max(values)
    return (b.high + tol >= max(b.open, b.close, b.low)
            and b.low - tol <= min(b.open, b.close, b.high))


def clean_series(symbol: str, bars: list[Bar], quality: DataQuality, intraday: bool,
                 bar_minutes: int = 5) -> list[Bar]:
    """Sorted, de-duplicated, valid bars. Every problem is counted in `quality`."""
    if not bars:
        return []
    keys = [b.date for b in bars]
    if any(keys[i] > keys[i + 1] for i in range(len(keys) - 1)):
        quality.unordered_series += 1
        quality.warn(f"{symbol}: bars arrived out of time order (sorted before use)")
    out, seen = [], set()
    for b in sorted(bars, key=lambda x: x.date):
        if b.date in seen:
            quality.duplicate_bars += 1
            continue
        seen.add(b.date)
        if not _valid_prices(b):
            quality.invalid_bars += 1
            continue
        if intraday:
            t = b.date.astimezone(ET)
            if (t.hour * 60 + t.minute) % bar_minutes or t.second:
                quality.off_grid_bars += 1
                continue
        out.append(b)
    return out


def check_session(symbol: str, day: dt.date, bars: list[Bar], quality: DataQuality, bar: str = "5m"):
    """Count regular-hours bars; a session with fewer than expected is reported (early
    closes such as the day after Thanksgiving also show up here)."""
    rth = sum(1 for b in bars if RTH_OPEN <= b.date.astimezone(ET).time() < RTH_CLOSE)
    if rth < rth_bar_count(bar):
        quality.incomplete_sessions.append([symbol, day.isoformat(), rth])


def aggregate(bars: list[Bar], minutes: int) -> list[Bar]:
    """1-minute bars -> `minutes` bars aligned to the clock (09:30, 09:35, ...)."""
    if minutes == 1:
        return list(bars)
    groups: dict[dt.datetime, list[Bar]] = {}
    for b in bars:
        t = b.date.astimezone(ET)
        floor = (t.hour * 60 + t.minute) // minutes * minutes
        start = t.replace(hour=floor // 60, minute=floor % 60, second=0, microsecond=0)
        groups.setdefault(start, []).append(b)
    return [Bar(start.astimezone(UTC), g[0].open, max(x.high for x in g), min(x.low for x in g),
                g[-1].close, sum(x.volume for x in g)) for start, g in sorted(groups.items())]


# ---------------------------------------------------------------- providers

class DataProvider:
    name = "base"
    synthetic = False
    description = ""

    def daily(self, symbol: str, start: dt.date, end: dt.date) -> list[Bar]:
        raise NotImplementedError

    def intraday(self, symbol: str, days: list[dt.date], bar: str = "5m") -> dict[dt.date, list[Bar]]:
        """Bars of size `bar`, 04:00-16:00 ET, for each requested day ([] = no data)."""
        raise NotImplementedError

    def limitations(self) -> list[str]:
        return []

    def revision(self, symbol: str, bar: str) -> str:
        """Changes when this provider's intraday data for `symbol` changes (uncached
        providers only; the cache tracks corrections itself)."""
        return ""

    def close(self):
        pass


class InMemoryProvider(DataProvider):
    """Hand-built bars for tests: {symbol: [Bar]} daily and {symbol: {day: [Bar]}} intraday
    (already in the bar size the test asks for)."""
    name = "in-memory (test data)"
    synthetic = True
    description = "Hand-built test bars. NOT market data."

    def __init__(self, daily: dict, intraday: dict):
        self._daily, self._intraday = daily, intraday
        self.requests: list = []

    def daily(self, symbol, start, end):
        self.requests.append(("daily", symbol))
        return [b for b in self._daily.get(symbol, []) if start <= b.date <= end]

    def intraday(self, symbol, days, bar="5m"):
        self.requests.append(("intraday", symbol, tuple(days), bar))
        return {d: list(self._intraday.get(symbol, {}).get(d, [])) for d in days}

    def revision(self, symbol, bar):
        days = self._intraday.get(symbol, {})
        return _hash(repr(sorted((d, _bar_tuples(days[d])) for d in days)))


class SyntheticProvider(DataProvider):
    """Deterministic generated prices for demos and tests. NOT market data.

    Daily opens/closes follow a seeded, mean-reverting random walk; about 1 day in
    12 opens with a 3-7% gap and trends up on heavy volume. Each day's 1-minute path
    (premarket and regular hours) is a seeded Brownian bridge from the open to the
    close; other bar sizes and the daily high/low are aggregated from it, so daily
    and intraday data always agree. Weekdays only, no holidays."""
    name = "synthetic"
    synthetic = True
    description = "Generated prices (seeded random walk). NOT market data; illustration only."
    EPOCH = dt.date(2023, 1, 2)

    def __init__(self, seed: int = 11):
        self.seed = seed
        self._days: dict[str, dict] = {}
        self._paths: dict[tuple[str, dt.date], list[Bar]] = {}

    def _rng(self, *parts):
        import random
        key = hashlib.sha256(":".join(map(str, (self.seed, *parts))).encode()).hexdigest()
        return random.Random(int(key[:16], 16))

    def _skeleton(self, symbol: str, until: dt.date) -> dict:
        cached = self._days.get(symbol)
        if cached and cached["until"] >= until:
            return cached["days"]
        rng = self._rng(symbol)
        base = price = 20 + rng.random() * 380
        days, day = {}, self.EPOCH
        while day <= until:
            if day.weekday() < 5:
                r = self._rng(symbol, day.isoformat())
                gap_day = r.random() < 1 / 12
                gap = r.uniform(0.03, 0.07) if gap_day else r.gauss(0.0005, 0.008)
                drift = r.uniform(0.005, 0.03) if gap_day else r.gauss(0, 0.009)
                op = price * (1 + gap)
                close = op * (1 + drift)
                days[day] = {"prev": price, "open": op, "close": close, "gap_day": gap_day}
                price = close * math.exp(-0.03 * math.log(close / base))  # bounded walk
            day += dt.timedelta(days=1)
        self._days[symbol] = {"until": until, "days": days}
        return days

    def _minutes(self, symbol: str, day: dt.date) -> list[Bar]:
        key = (symbol, day)
        if key in self._paths:
            return self._paths[key]
        info = self._skeleton(symbol, day).get(day)
        if info is None:
            return []
        r = self._rng(symbol, day.isoformat(), "path")
        bars = []

        def bridge(start_price, end_price, n, vol):
            walk, x = [], 0.0
            for _ in range(n):
                x += r.gauss(0, vol)
                walk.append(x)
            return [start_price + (end_price - start_price) * (i + 1) / n + walk[i] - walk[-1] * (i + 1) / n
                    for i in range(n)]

        pre = bridge(info["prev"], info["open"] * 0.997, 330, info["prev"] * 0.0006)
        rth = bridge(info["open"], info["close"], 390, info["open"] * 0.0011)
        start = dt.datetime.combine(day, PRE_OPEN, ET)
        prev = info["prev"]
        for i, p in enumerate(pre + rth):
            t = start + dt.timedelta(minutes=i)
            o = info["open"] if i == 330 else prev
            c = max(0.5, p)
            hi = max(o, c) * (1 + abs(r.gauss(0, 0.0004)))
            lo = min(o, c) * (1 - abs(r.gauss(0, 0.0004)))
            if i < 330:
                v = r.randint(50, 400) * (3 if info["gap_day"] else 1)
            else:
                m = i - 330
                u = math.exp(-m / 40) + math.exp(-(389 - m) / 40)  # U-shaped volume
                v = int((8000 + 30000 * u) * (3 if info["gap_day"] else 1) * r.uniform(0.7, 1.3))
            bars.append(Bar(t.astimezone(UTC), round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), float(v)))
            prev = c
        self._paths[key] = bars
        return bars

    def daily(self, symbol, start, end):
        out = []
        for day in sorted(d for d in self._skeleton(symbol, end) if start <= d <= end):
            rth = [b for b in self._minutes(symbol, day) if b.date.astimezone(ET).time() >= RTH_OPEN]
            out.append(Bar(day, rth[0].open, max(b.high for b in rth), min(b.low for b in rth),
                           rth[-1].close, sum(b.volume for b in rth)))
        return out

    def intraday(self, symbol, days, bar="5m"):
        return {d: aggregate(self._minutes(symbol, d), BAR_MINUTES[bar]) for d in days}

    def limitations(self):
        return ["SYNTHETIC DATA: prices are generated, not historical. Results illustrate the "
                "software only and say nothing about the strategy."]


class YFinanceProvider(DataProvider):
    """Yahoo Finance via yfinance. Short intraday history (see YAHOO_DAYS)."""
    name = "yfinance"
    description = "Yahoo Finance (yfinance): daily bars for years, intraday bars for weeks."

    def __init__(self):
        import yfinance  # noqa: F401 - fail early with a clear ImportError
        self._notes: list[str] = []

    @staticmethod
    def _yahoo(symbol: str) -> str:
        return symbol.strip().upper().replace(" ", "-").replace(".", "-")

    @staticmethod
    def _frame(df, ysym):
        import pandas as pd
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            level = 1 if ysym in df.columns.get_level_values(1) else 0
            df = df.xs(ysym, axis=1, level=level)
        return df

    def daily(self, symbol, start, end):
        import yfinance as yf
        ysym = self._yahoo(symbol)
        df = self._frame(yf.download(ysym, start=start.isoformat(),
                                     end=(end + dt.timedelta(days=1)).isoformat(), interval="1d",
                                     auto_adjust=False, progress=False, threads=False), ysym)
        if df is None:
            return []
        out = []
        for ts, row in df.iterrows():
            values = [_float(row.get(k)) for k in ("Open", "High", "Low", "Close", "Volume")]
            if None in values[:4]:
                continue
            out.append(Bar(ts.date(), *values[:4], values[4] or 0.0))
        return out

    @staticmethod
    def earliest(bar: str) -> dt.date:
        return today_et() - dt.timedelta(days=YAHOO_DAYS[bar])

    def intraday(self, symbol, days, bar="5m"):
        import yfinance as yf
        out = {d: [] for d in days}
        earliest = self.earliest(bar)
        wanted = sorted(d for d in days if d >= earliest)
        if len(wanted) < len(days):
            self._notes.append(f"Yahoo has no {bar} history before {earliest}; "
                               f"{len(days) - len(wanted)} requested symbol-days are unavailable")
        ysym = self._yahoo(symbol)
        chunk = dt.timedelta(days=YAHOO_CHUNK[bar])
        i = 0
        while i < len(wanted):
            first = wanted[i]
            last = max(d for d in wanted if d < first + chunk)
            i = wanted.index(last) + 1
            df = self._frame(yf.download(ysym, start=first.isoformat(),
                                         end=(last + dt.timedelta(days=1)).isoformat(),
                                         interval=bar, prepost=True, auto_adjust=False,
                                         progress=False, threads=False), ysym)
            if df is None:
                continue
            for ts, row in df.iterrows():
                t = ts.to_pydatetime()
                t = t.replace(tzinfo=UTC) if t.tzinfo is None else t
                local = t.astimezone(ET)
                if local.date() not in out or not (PRE_OPEN <= local.time() < RTH_CLOSE):
                    continue
                values = [_float(row.get(k)) for k in ("Open", "High", "Low", "Close", "Volume")]
                if None in values[:4]:
                    continue
                out[local.date()].append(Bar(t.astimezone(UTC), *values[:4], values[4] or 0.0))
        return out

    def limitations(self):
        return sorted(set(self._notes)) + [
            "Yahoo Finance prices may differ slightly from IBKR's (consolidated vs. IBKR feed); "
            "premarket volume on Yahoo is often incomplete."]


class IBKRProvider(DataProvider):
    """IBKR historical bars over a READ-ONLY API connection (paper ports only).

    Uses config.json's paper connection with client_id + client_id_offset, so it never
    takes the bot's client id. Every order method on the connection object raises."""
    name = "ibkr"
    description = "Interactive Brokers historical TRADES bars (same source as the live bot)."
    ORDER_METHODS = ("placeOrder", "cancelOrder", "reqGlobalCancel", "bracketOrder",
                     "oneCancelsAll", "whatIfOrder", "placeOrderAsync", "whatIfOrderAsync")
    PAPER_PORTS = {4002, 7497, 4004}
    CHUNK_DAYS = {"1m": 1, "5m": 7, "15m": 7, "1h": 7}

    def __init__(self, connection: dict, settings: dict):
        from ib_async import IB  # imported only when this provider is used
        port = connection.get("port")
        if port not in self.PAPER_PORTS:
            raise DataError(f"IBKR provider refuses port {port}: backtests only connect to a "
                            f"paper port {sorted(self.PAPER_PORTS)}")
        self._settings = settings
        self._contracts: dict = {}
        self.ib = IB()
        client_id = int(connection.get("client_id", 21)) + int(settings["client_id_offset"])
        try:
            self.ib.connect(connection.get("host", "127.0.0.1"), port, clientId=client_id,
                            readonly=True, timeout=15)
        except Exception as e:  # noqa: BLE001 - any failure: no data from this provider
            raise DataError(f"cannot connect to IB Gateway at {connection.get('host')}:{port} "
                            f"(clientId {client_id}, read-only): {e!r}") from None
        accounts = self.ib.managedAccounts()
        if len(accounts) != 1 or not accounts[0].startswith("D"):
            self.ib.disconnect()
            raise DataError("IBKR provider refuses to use a non-paper account")
        for name in self.ORDER_METHODS:
            setattr(self.ib, name, self._blocked(name))

    @staticmethod
    def _blocked(name):
        def refuse(*args, **kwargs):
            raise RuntimeError(f"ib.{name} is disabled: backtests never send orders")
        return refuse

    def _contract(self, symbol):
        from ib_async import Stock
        if symbol not in self._contracts:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            if not contract.conId:
                raise SymbolNotFound(f"IBKR could not qualify {symbol}")
            self._contracts[symbol] = contract
        return self._contracts[symbol]

    def _request(self, contract, end: str, duration: str, size: str, rth: bool):
        bars = self.ib.reqHistoricalData(contract, end, duration, size, "TRADES", useRTH=rth,
                                         formatDate=2, timeout=self._settings["timeout_seconds"])
        self.ib.sleep(self._settings["request_pause_seconds"])
        return bars or []

    def daily(self, symbol, start, end):
        contract = self._contract(symbol)
        span = (end - start).days + 1
        duration = f"{math.ceil(span / 365)} Y" if span > 365 else f"{span} D"
        bars = self._request(contract, f"{end:%Y%m%d} 23:59:59 US/Eastern", duration, "1 day", True)
        out = []
        for b in bars:
            day = b.date if isinstance(b.date, dt.date) and not isinstance(b.date, dt.datetime) \
                else b.date.astimezone(ET).date()
            if start <= day <= end:
                out.append(Bar(day, float(b.open), float(b.high), float(b.low), float(b.close),
                               float(b.volume)))
        return out

    def intraday(self, symbol, days, bar="5m"):
        contract = self._contract(symbol)
        out = {d: [] for d in days}
        chunks, chunk = [], []
        for d in sorted(days):
            if chunk and (d - chunk[0]).days >= self.CHUNK_DAYS[bar]:
                chunks.append(chunk)
                chunk = []
            chunk.append(d)
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            span = (chunk[-1] - chunk[0]).days + 1
            bars = self._request(contract, f"{chunk[-1]:%Y%m%d} 16:00:00 US/Eastern",
                                 f"{span} D", IBKR_SIZES[bar], False)
            for b in bars:
                if not isinstance(b.date, dt.datetime):
                    continue
                local = b.date.astimezone(ET)
                if local.date() in out and PRE_OPEN <= local.time() < RTH_CLOSE:
                    out[local.date()].append(Bar(b.date.astimezone(UTC), float(b.open), float(b.high),
                                                 float(b.low), float(b.close), float(b.volume)))
        return out

    def limitations(self):
        return ["IBKR historical volume units are assumed to be shares; if your feed reports "
                "lots of 100, only the participation cap (not RVOL, a ratio) is affected."]

    def close(self):
        try:
            self.ib.disconnect()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- cache

INTRADAY_FIELDS = ["ts", "open", "high", "low", "close", "volume"]
DAILY_FIELDS = ["date", "open", "high", "low", "close", "volume"]


def _csv_text(fields: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    writer.writerows(rows)
    return buf.getvalue()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _bar_tuples(bars) -> list[tuple]:
    return [(b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars]


class DataCache:
    """What a real provider returned, on disk: <root>/<provider>/1d/<SYM>.csv and
    <root>/<provider>/<bar>/<SYM>/<YYYY-MM-DD>.csv, plus _index.json per symbol.
    Days on or after today (ET) are never cached (incomplete)."""

    def __init__(self, root: Path, provider: DataProvider, refresh: bool = False, bar: str = "5m"):
        self.root = root / provider.name.split()[0]
        self.provider, self.refresh, self.bar = provider, refresh, bar
        self._changes: dict | None = None  # (symbol, bar) -> corrections recorded

    @staticmethod
    def _safe(symbol: str) -> str:
        return symbol.upper().replace(" ", "_").replace("/", "_")

    def _index_path(self, symbol):
        return self.root / self.bar / self._safe(symbol) / "_index.json"

    def _index(self, symbol) -> dict:
        try:
            return json.loads(self._index_path(symbol).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def revision(self, symbol: str) -> str:
        """How many times a cached intraday day of `symbol` was corrected by the provider
        (changes.jsonl): part of the dataset identity, so corrected data is a new dataset."""
        if self._changes is None:
            self._changes = {}
            try:
                lines = (self.root / "changes.jsonl").read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    c = json.loads(line)
                except ValueError:
                    continue
                key = (c.get("symbol"), c.get("bar"))
                self._changes[key] = self._changes.get(key, 0) + 1
        return str(self._changes.get((symbol, self.bar), 0))

    def _record_change(self, symbol, day, old, new):
        path = self.root / "changes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": dt.datetime.now(UTC).isoformat(timespec="seconds"),
                                "symbol": symbol, "bar": self.bar, "day": str(day),
                                "old_hash": old, "new_hash": new}) + "\n")
        if self._changes is not None:
            key = (symbol, self.bar)
            self._changes[key] = self._changes.get(key, 0) + 1

    def intraday(self, symbol: str, days: list[dt.date]) -> tuple[dict, dict]:
        """({day: [Bar]}, {day: content hash}) for the requested days."""
        index = self._index(symbol)
        today = today_et()
        days = [d for d in days if d < today]
        missing = [d for d in days if self.refresh or d.isoformat() not in index]
        if missing:
            fetched = self.provider.intraday(symbol, missing, self.bar)
            folder = self._index_path(symbol).parent
            folder.mkdir(parents=True, exist_ok=True)
            for day, bars in fetched.items():
                rows = [[int(b.date.timestamp()), b.open, b.high, b.low, b.close, b.volume]
                        for b in sorted(bars, key=lambda b: b.date)]
                text = _csv_text(INTRADAY_FIELDS, rows)
                h = _hash(text)
                old = index.get(day.isoformat(), {}).get("hash")
                if old and old != h:
                    self._record_change(symbol, day, old, h)
                if rows:
                    (folder / f"{day.isoformat()}.csv").write_text(text, encoding="utf-8")
                index[day.isoformat()] = {"rows": len(rows), "hash": h,
                                          "fetched_at": dt.datetime.now(UTC).isoformat(timespec="seconds")}
            self._index_path(symbol).write_text(json.dumps(index, indent=0), encoding="utf-8")
        out, hashes = {}, {}
        folder = self._index_path(symbol).parent
        for day in days:
            meta = index.get(day.isoformat())
            if not meta:
                out[day] = []
                continue
            hashes[day] = meta["hash"]
            out[day] = self._read_day(folder / f"{day.isoformat()}.csv") if meta["rows"] else []
        return out, hashes

    @staticmethod
    def _read_day(path: Path) -> list[Bar]:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                return [Bar(dt.datetime.fromtimestamp(int(r["ts"]), UTC), float(r["open"]),
                            float(r["high"]), float(r["low"]), float(r["close"]),
                            float(r["volume"])) for r in csv.DictReader(f)]
        except (OSError, ValueError, KeyError) as e:
            raise DataError(f"corrupt cache file {path}: {e}") from None

    def daily(self, symbol: str, start: dt.date, end: dt.date) -> tuple[list[Bar], str]:
        end = min(end, today_et() - dt.timedelta(days=1))
        folder = self.root / "1d"
        path, meta_path = folder / f"{self._safe(symbol)}.csv", folder / f"{self._safe(symbol)}.meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        covered = (meta and not self.refresh and meta["start"] <= start.isoformat()
                   and meta["end"] >= end.isoformat())
        if not covered:
            bars = self.provider.daily(symbol, start, end)
            existing = {} if self.refresh else {b.date: b for b in self._read_daily(path)}
            existing.update({b.date: b for b in bars})
            merged = [existing[d] for d in sorted(existing)]
            text = _csv_text(DAILY_FIELDS, [[b.date.isoformat(), b.open, b.high, b.low, b.close,
                                             b.volume] for b in merged])
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            lo = min(start.isoformat(), meta.get("start", start.isoformat())) if meta else start.isoformat()
            hi = max(end.isoformat(), meta.get("end", end.isoformat())) if meta else end.isoformat()
            meta_path.write_text(json.dumps({"start": lo, "end": hi, "fetched_at":
                                             dt.datetime.now(UTC).isoformat(timespec="seconds")}),
                                 encoding="utf-8")
        bars = [b for b in self._read_daily(path) if start <= b.date <= end]
        text = _csv_text(DAILY_FIELDS, [[b.date.isoformat(), b.open, b.high, b.low, b.close,
                                         b.volume] for b in bars])
        return bars, _hash(text)

    @staticmethod
    def _read_daily(path: Path) -> list[Bar]:
        if not path.exists():
            return []
        try:
            with open(path, newline="", encoding="utf-8") as f:
                return [Bar(dt.date.fromisoformat(r["date"]), float(r["open"]), float(r["high"]),
                            float(r["low"]), float(r["close"]), float(r["volume"]))
                        for r in csv.DictReader(f)]
        except (OSError, ValueError, KeyError) as e:
            raise DataError(f"corrupt cache file {path}: {e}") from None


class MarketData:
    """What the simulator reads: cleaned daily bars and per-day intraday bars of one
    size, loaded on demand through the cache (real providers) or straight from the
    provider (synthetic / test data, never cached). Records every content hash used."""

    def __init__(self, provider: DataProvider, cache_root: Path | None, refresh: bool = False,
                 bar: str = "5m"):
        if bar not in BAR_MINUTES:
            raise DataError(f"unsupported intraday bar size {bar!r}")
        self.provider, self.bar = provider, bar
        self.cache = None if (provider.synthetic or cache_root is None) else \
            DataCache(cache_root, provider, refresh, bar)
        self.quality = DataQuality(provider=provider.name, synthetic=provider.synthetic, bar=bar)
        self.hashes: dict[str, str] = {}
        self._daily: dict[str, list[Bar]] = {}
        self._days: dict[tuple[str, dt.date], list[Bar]] = {}

    def load_daily(self, symbols: list[str], start: dt.date, end: dt.date) -> dict[str, list[Bar]]:
        for i, symbol in enumerate(symbols, 1):
            try:
                if self.cache:
                    bars, h = self.cache.daily(symbol, start, end)
                else:
                    bars = self.provider.daily(symbol, start, end)
                    h = _hash(repr(_bar_tuples(bars)))
            except DataError:
                raise
            except Exception as e:  # noqa: BLE001 - one symbol's failure is reported, not fatal
                self.quality.warn(f"{symbol}: daily data request failed: {e!r}")
                bars, h = [], ""
            self.hashes[f"{symbol}:1d"] = h
            self._daily[symbol] = clean_series(symbol, bars, self.quality, intraday=False)
            if i % 25 == 0:
                log.info("daily bars: %d/%d symbols", i, len(symbols))
        return self._daily

    def daily(self, symbol: str) -> list[Bar]:
        return self._daily.get(symbol, [])

    def revision(self, symbol: str) -> str:
        """Identifies the version of `symbol`'s intraday data (see DataCache.revision)."""
        return self.cache.revision(symbol) if self.cache else self.provider.revision(symbol, self.bar)

    def prefetch(self, needed: dict[str, set]):
        """Fetch (and cache) every symbol-day the run will need, one batch per symbol."""
        total = len(needed)
        for i, (symbol, days) in enumerate(sorted(needed.items()), 1):
            missing = sorted(d for d in days if (symbol, d) not in self._days)
            if missing:
                self._load(symbol, missing)
            if i % 10 == 0 or i == total:
                log.info("intraday %s bars: %d/%d symbols", self.bar, i, total)

    def _load(self, symbol: str, days: list[dt.date]):
        try:
            if self.cache:
                got, hashes = self.cache.intraday(symbol, days)
            else:
                got = self.provider.intraday(symbol, days, self.bar)
                hashes = {d: _hash(repr(_bar_tuples(bars))) for d, bars in got.items()}
        except DataError:
            raise
        except Exception as e:  # noqa: BLE001
            self.quality.warn(f"{symbol}: intraday data request failed: {e!r}")
            got, hashes = {d: [] for d in days}, {}
        minutes = BAR_MINUTES[self.bar]
        for day in days:
            bars = clean_series(symbol, got.get(day, []), self.quality, intraday=True,
                                bar_minutes=minutes)
            # timestamps in the ET zone object the core uses: its astimezone(ET) is then free
            bars = [Bar(b.date.astimezone(ET), b.open, b.high, b.low, b.close, b.volume)
                    for b in bars if b.date.astimezone(ET).date() == day]
            self._days[(symbol, day)] = bars
            self.hashes[f"{symbol}:{self.bar}:{day.isoformat()}"] = hashes.get(day, "")
            if bars:
                self.quality.intraday_days_loaded += 1
                check_session(symbol, day, bars, self.quality, self.bar)
            else:
                self.quality.intraday_days_missing.append([symbol, day.isoformat()])

    def day_bars(self, symbol: str, day: dt.date) -> list[Bar]:
        if (symbol, day) not in self._days:
            self._load(symbol, [day])
        return self._days[(symbol, day)]

    def release(self, before: dt.date):
        """Drop intraday bars of days before `before` (memory)."""
        for key in [k for k in self._days if k[1] < before]:
            del self._days[key]
