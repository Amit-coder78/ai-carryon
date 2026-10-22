"""Stage 1: topic selection. Never invents trends: news needs fresh research items; evergreen uses a fixed curriculum."""
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from . import calculations as calc
from .facts import Fact

EVERGREEN = {
    "indian_markets": ["How the Nifty 50 index is built", "What market breadth tells you about a rally", "Sensex versus Nifty: what is the difference"],
    "global_markets": ["Why US markets move Indian markets", "How currency moves affect foreign investors", "What a stock index actually measures"],
    "company_analysis": ["How to read a company's profit and loss statement", "P/E ratio: what it says and what it hides", "Why debt and cash flow matter more than headlines"],
    "ipo": ["How an IPO price band works", "Reading an IPO's offer document: three things to check", "Why listing day gains are never guaranteed"],
    "technical_analysis": ["How to read a candlestick chart", "Support and resistance: useful lens, not a rule", "Why stop losses and position sizing matter"],
    "derivatives": ["Call and put options explained simply", "Why leverage in futures cuts both ways", "What option premium and expiry mean"],
    "investing": ["How compounding works over time", "Diversification: why not one basket", "Index funds versus picking stocks"],
    "mutual_funds_etfs": ["SIP versus lump sum: how they differ", "What an expense ratio does to returns", "What NAV really means"],
    "macro": ["How interest rates affect stock prices", "What inflation does to your savings", "Why bond yields matter to equity investors"],
    "news": ["How to read an earnings announcement", "What a central bank rate decision does and does not tell you", "Rumour versus confirmed news in markets"],
    "sectors": ["What a sector index is", "Why banks and IT react differently to rates", "Cyclical versus defensive sectors"],
    "history": ["The Harshad Mehta scam: how it worked", "How the 2008 financial crisis unfolded", "The dot-com bubble in simple terms"],
    "psychology_personal_finance": ["Fear, greed and herd behaviour in markets", "Why an emergency fund comes before investing", "The most common beginner investing mistakes"],
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


@dataclass
class FinanceTopic:
    id: str
    category: str
    content_type: str
    market: str
    title: str
    ticker: str = ""
    sources: list = field(default_factory=list)   # SourceRecord dicts from research
    entity_terms: list = field(default_factory=list)  # names/tickers that must never appear in stock-footage queries

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, d): return cls(**d)


def requires_current_data(settings):
    return settings.content_type in ("news", "analytical")


def select_topic(settings, research_items, posted_ids, now=None):
    """Returns a FinanceTopic or None (skip: nothing grounded to say)."""
    now = now or datetime.now(timezone.utc)
    common = dict(category=settings.category, content_type=settings.content_type, market=settings.market,
                  ticker=settings.ticker, entity_terms=[settings.ticker] if settings.ticker else [])
    if settings.content_type == "news":
        for it in research_items:
            tid = f"news:{slug(it['title'])}"
            if tid not in posted_ids:
                return FinanceTopic(id=tid, title=it["title"], sources=[it["source"]], **common)
        return None
    if settings.content_type == "analytical":
        if not settings.ticker: return None
        tid = f"analysis:{settings.ticker}:{now.date().isoformat()}"
        return None if tid in posted_ids else FinanceTopic(id=tid, title=f"{settings.ticker}: what the numbers show", **common)
    for title in EVERGREEN[settings.category]:
        tid = f"{settings.content_type}:{settings.category}:{slug(title)}"
        if tid not in posted_ids:
            return FinanceTopic(id=tid, title=title, **common)
    return None


def example_facts(topic):
    """Deterministic, clearly hypothetical numbers for evergreen explainers."""
    if topic.category in ("investing", "mutual_funds_etfs"):
        monthly, rate, years = 5000, 12, 10
        fv = calc.sip_future_value(monthly, rate, years)
        note = f"Assumes {monthly} INR invested monthly for {years} years at a constant {rate}% annual return. Real returns vary and can be negative."
        return [Fact("ex_monthly", "Example monthly investment", monthly, "hypothetical", "money", "INR", 0, note=note),
                Fact("ex_rate", "Example assumed annual return", rate, "hypothetical", "pct", decimals=0, note=note),
                Fact("ex_years", "Example investment period in years", years, "hypothetical", "count", decimals=0, note=note),
                Fact("ex_fv", "Example projected value", fv, "computed", "money", "INR", 0, note=note,
                     derived_from=["ex_monthly", "ex_rate", "ex_years"])]
    if topic.category in ("technical_analysis", "derivatives"):
        entry, stop, target = 100, 95, 110
        note = f"Illustrative prices only: entry {entry}, stop {stop}, target {target}. Not a real trade."
        return [Fact("ex_entry", "Example entry price", entry, "hypothetical", "count", decimals=0, note=note),
                Fact("ex_stop", "Example stop-loss price", stop, "hypothetical", "count", decimals=0, note=note),
                Fact("ex_target", "Example target price", target, "hypothetical", "count", decimals=0, note=note),
                Fact("ex_rr", "Example risk-to-reward ratio", calc.risk_reward(entry, stop, target), "computed", "x",
                     decimals=1, note=note, derived_from=["ex_entry", "ex_stop", "ex_target"])]
    return []
