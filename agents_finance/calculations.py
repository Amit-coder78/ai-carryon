"""Deterministic finance math. The language model never computes numbers; it only references them."""
import math


class CalcError(ValueError):
    pass


def _n(x, name):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise CalcError(f"{name} must be a finite number, got {x!r}")
    return float(x)


def _pos(x, name):
    x = _n(x, name)
    if x <= 0:
        raise CalcError(f"{name} must be > 0, got {x}")
    return x


def pct_change(old, new):
    old, new = _pos(old, "old"), _n(new, "new")
    return (new - old) / old * 100.0


def cagr(begin, end, years):
    begin, end, years = _pos(begin, "begin"), _pos(end, "end"), _pos(years, "years")
    return ((end / begin) ** (1.0 / years) - 1.0) * 100.0


def market_cap(price, shares):
    return _pos(price, "price") * _pos(shares, "shares")


def dividend_yield(dividend_per_share, price):
    d = _n(dividend_per_share, "dividend_per_share")
    if d < 0: raise CalcError("dividend_per_share must be >= 0")
    return d / _pos(price, "price") * 100.0


def pe_ratio(price, eps):
    eps = _n(eps, "eps")
    if eps <= 0: raise CalcError("EPS <= 0: P/E is not meaningful")
    return _pos(price, "price") / eps


def pb_ratio(price, book_value_per_share):
    bv = _n(book_value_per_share, "book_value_per_share")
    if bv <= 0: raise CalcError("book value <= 0: P/B is not meaningful")
    return _pos(price, "price") / bv


def ev_ebitda(enterprise_value, ebitda):
    e = _n(ebitda, "ebitda")
    if e <= 0: raise CalcError("EBITDA <= 0: EV/EBITDA is not meaningful")
    return _n(enterprise_value, "enterprise_value") / e


def convert_currency(amount, rate):
    """rate = units of target currency per 1 unit of source currency."""
    return _n(amount, "amount") * _pos(rate, "rate")


def lumpsum_future_value(principal, annual_rate_pct, years):
    return _pos(principal, "principal") * (1 + _n(annual_rate_pct, "rate") / 100.0) ** _pos(years, "years")


def sip_future_value(monthly, annual_rate_pct, years):
    """Hypothetical SIP value: contributions at the START of each month (annuity-due),
    monthly rate = annual/12. Always label as a hypothetical projection."""
    m, r, y = _pos(monthly, "monthly"), _n(annual_rate_pct, "rate"), _pos(years, "years")
    n = int(round(y * 12))
    if n < 1: raise CalcError("years too small")
    i = r / 100.0 / 12.0
    if i == 0:
        return m * n
    return m * (((1 + i) ** n - 1) / i) * (1 + i)


def risk_reward(entry, stop, target):
    entry, stop, target = _pos(entry, "entry"), _pos(stop, "stop"), _pos(target, "target")
    if (stop < entry) != (target > entry) or stop == entry or target == entry:
        raise CalcError("stop and target must be on opposite sides of entry")
    return abs(target - entry) / abs(entry - stop)


def position_size(capital, risk_pct, entry, stop):
    capital, risk_pct = _pos(capital, "capital"), _pos(risk_pct, "risk_pct")
    if risk_pct > 100: raise CalcError("risk_pct must be <= 100")
    entry, stop = _pos(entry, "entry"), _pos(stop, "stop")
    if entry == stop: raise CalcError("entry and stop must differ")
    return int(math.floor(capital * risk_pct / 100.0 / abs(entry - stop)))


def sma(values, period):
    if period < 1 or len(values) < period: raise CalcError("not enough data for SMA")
    return [sum(values[i - period + 1:i + 1]) / period for i in range(period - 1, len(values))]


def rsi(closes, period=14):
    """Wilder's RSI of the last bar. Descriptive only, never a predictor."""
    if len(closes) < period + 1: raise CalcError("not enough data for RSI")
    gains = losses = 0.0
    for a, b in zip(closes[:period], closes[1:period + 1]):
        d = b - a
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / period, losses / period
    for a, b in zip(closes[period:], closes[period + 1:]):
        d = b - a
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    return 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
