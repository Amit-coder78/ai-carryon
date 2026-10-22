"""Finance-only settings. Reads FINANCE_* env vars only; never touches other channels' config."""
import os
from dataclasses import dataclass

CHANNEL = "finance"
CHANNEL_NAME = "AI CarryON Finance"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    needs_live_data: bool       # topic is normally about current numbers
    needs_risk_statement: bool  # script must state risk of loss
    stock_terms: tuple          # GENERIC footage queries only (never entity-specific)


def _c(key, label, live, risk, *terms):
    return Category(key, label, live, risk, terms)


_GEN = ("stock market trading screen", "financial charts monitor", "business district skyline")
CATEGORIES = {c.key: c for c in (
    _c("indian_markets", "Indian stock market", True, False, "stock market trading screen", "mumbai skyline", "financial charts monitor"),
    _c("global_markets", "Global stock markets", True, False, "stock exchange trading floor", "city skyline night", "financial charts monitor"),
    _c("company_analysis", "Individual stocks and company analysis", True, True, "office building business", "financial report documents", "calculator desk"),
    _c("ipo", "IPOs and new listings", True, True, "stock exchange bell", "business meeting", "financial documents"),
    _c("technical_analysis", "Trading and technical analysis", False, True, "candlestick chart screen", "trader multiple monitors", "stock chart"),
    _c("derivatives", "Futures and options", False, True, "trader multiple monitors", "financial charts monitor", "stock chart"),
    _c("investing", "Investing and portfolio management", False, True, "plant growing coins", "piggy bank savings", "financial planning desk"),
    _c("mutual_funds_etfs", "Mutual funds and ETFs", False, True, "financial planning desk", "calculator desk", "investment portfolio"),
    _c("macro", "Economics and macroeconomics", True, False, "central bank building", "currency notes", "city traffic economy"),
    _c("news", "Financial news", True, False, "newsroom screens", "financial charts monitor", "stock market trading screen"),
    _c("sectors", "Sector analysis", True, False, "factory production line", "city skyline business", "financial charts monitor"),
    _c("history", "Financial history and business stories", False, False, "old newspaper", "vintage stock ticker", "library books"),
    _c("psychology_personal_finance", "Investor psychology and personal finance", False, False, "person thinking desk", "budget planning notebook", "piggy bank savings"),
)}
CONTENT_TYPES = ("educational", "analytical", "historical", "news")
MARKETS = ("india", "global")
FORMATS = ("shorts", "long")


def _bool(v, default):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _int(v, default, name):
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {v!r}")


@dataclass(frozen=True)
class FinanceSettings:
    language: str = "en"
    market: str = "india"
    category: str = "indian_markets"
    ticker: str = ""
    content_type: str = "educational"
    video_format: str = "shorts"
    duration_sec: int = 55
    data_max_age_hours: int = 72      # 72h so a Friday close is still usable on Monday morning
    research_max_age_hours: int = 48
    subtitles: bool = True
    music: bool = True
    require_review: bool = True       # safe default: render + save for review, do not auto-upload
    max_retries: int = 2
    daily_upload_cap: int = 3
    words_per_second: float = 2.4

    @property
    def cat(self) -> Category:
        return CATEGORIES[self.category]

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env
        g = lambda k, d="": e.get("FINANCE_" + k, d)
        fmt = g("FORMAT", "shorts").lower()
        dur = _int(g("DURATION_SEC"), 55 if fmt == "shorts" else 300, "FINANCE_DURATION_SEC")
        s = cls(
            language=g("LANGUAGE", "en").lower(), market=g("MARKET", "india").lower(),
            category=g("CATEGORY", "indian_markets").lower(), ticker=g("TICKER").strip().upper(),
            content_type=g("CONTENT_TYPE", "educational").lower(), video_format=fmt, duration_sec=dur,
            data_max_age_hours=_int(g("DATA_MAX_AGE_HOURS"), 72, "FINANCE_DATA_MAX_AGE_HOURS"),
            research_max_age_hours=_int(g("RESEARCH_MAX_AGE_HOURS"), 48, "FINANCE_RESEARCH_MAX_AGE_HOURS"),
            subtitles=_bool(g("SUBTITLES"), True), music=_bool(g("MUSIC"), True),
            require_review=_bool(g("REQUIRE_REVIEW"), True),
            max_retries=_int(g("MAX_RETRIES"), 2, "FINANCE_MAX_RETRIES"),
            daily_upload_cap=_int(g("DAILY_UPLOAD_CAP"), 3, "FINANCE_DAILY_UPLOAD_CAP"))
        s.validate()
        return s

    def validate(self):
        if self.language != "en":
            raise ConfigError("Finance currently supports English only (FINANCE_LANGUAGE=en)")
        if self.market not in MARKETS: raise ConfigError(f"FINANCE_MARKET must be one of {MARKETS}")
        if self.category not in CATEGORIES: raise ConfigError(f"FINANCE_CATEGORY must be one of {sorted(CATEGORIES)}")
        if self.content_type not in CONTENT_TYPES: raise ConfigError(f"FINANCE_CONTENT_TYPE must be one of {CONTENT_TYPES}")
        if self.video_format not in FORMATS: raise ConfigError(f"FINANCE_FORMAT must be one of {FORMATS}")
        lo, hi = (45, 60) if self.video_format == "shorts" else (120, 1200)
        if not lo <= self.duration_sec <= hi:
            raise ConfigError(f"duration for {self.video_format} must be {lo}-{hi}s, got {self.duration_sec}")
        if self.max_retries < 0: raise ConfigError("FINANCE_MAX_RETRIES must be >= 0")
        if self.ticker and not __import__("re").fullmatch(r"[A-Z0-9&\-\.\^]{1,20}", self.ticker):
            raise ConfigError(f"invalid FINANCE_TICKER {self.ticker!r}")
