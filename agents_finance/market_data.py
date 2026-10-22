"""Market-data provider: Stooq daily CSV (free, no API key, stdlib only).
Limits: end-of-day data only (not live), coverage depends on Stooq symbols, personal/non-commercial
terms apply. VERIFY that the Indian symbols you need exist before relying on them. Provider is
swappable: anything with .daily_bars(symbol, days) -> list[Bar] works."""
import csv, io, urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from .calculations import pct_change
from .facts import Fact


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def _default_fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-carryon-finance/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


class StooqProvider:
    name = "stooq"

    def __init__(self, fetch=_default_fetch):
        self._fetch = fetch

    def page_url(self, symbol):
        return f"https://stooq.com/q/?s={symbol.lower()}"

    def daily_bars(self, symbol, days=30):
        url = f"https://stooq.com/q/d/l/?s={symbol.lower()}&i=d"
        try:
            text = self._fetch(url)
        except Exception as e:
            raise MarketDataError(f"fetch failed for {symbol}: {e}") from e
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or "Close" not in rows[0]:
            raise MarketDataError(f"no data for {symbol}")
        bars = []
        for r in rows:
            try:
                bars.append(Bar(date.fromisoformat(r["Date"]), float(r["Open"]), float(r["High"]),
                                float(r["Low"]), float(r["Close"]), float(r.get("Volume") or 0)))
            except (KeyError, ValueError):
                continue
        if len(bars) < 2:
            raise MarketDataError(f"insufficient bars for {symbol}")
        bars.sort(key=lambda b: b.day)
        return bars[-days:]


def _eod(d: date):
    return datetime.combine(d, time(23, 59), tzinfo=timezone.utc).isoformat()


def build_close_facts(prefix, label, symbol, bars, currency, provider: StooqProvider):
    """Returns [close fact, 1-day % change fact (computed), series chart fact]. Pure and deterministic."""
    last, prev = bars[-1], bars[-2]
    src = dict(source_url=provider.page_url(symbol), source_title=f"{provider.name} daily data: {symbol}")
    close = Fact(f"{prefix}_close", f"{label} latest close", last.close, "current",
                 unit="money" if currency else "count", currency=currency, as_of=_eod(last.day), **src)
    chg = Fact(f"{prefix}_chg", f"{label} change vs previous close", pct_change(prev.close, last.close), "computed",
               unit="pct", as_of=_eod(last.day), derived_from=[close.id], **src)
    series = Fact(f"{prefix}_series", f"{label} closing prices", f"{len(bars)} daily closes", "historical",
                  unit="text", as_of=_eod(last.day), series=[[b.day.isoformat(), b.close] for b in bars], **src)
    return [close, chg, series]
