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


# More evergreen explainers per category, so a daily schedule does not run dry after three videos.
# Conceptual only (no live numbers, no advice); every script still goes through the fact-checker.
_MORE_EVERGREEN = {
    "indian_markets": [
        "What the India VIX measures", "How circuit breakers work on Indian exchanges",
        "How settlement works after you buy a share", "Large, mid and small cap: what the labels mean",
        "How an index is rebalanced", "Why trading volume matters", "What the bid and ask price mean",
        "What a stock split does and does not change", "Dividends explained: record date and ex-date"],
    "global_markets": [
        "What the US Federal Reserve does and why markets watch it", "How the dollar-rupee exchange rate moves",
        "Why oil prices ripple through stock markets", "What emerging markets means to investors",
        "How foreign portfolio investors affect Indian markets", "What a recession is and how markets react",
        "Why global cues shape the Indian market open", "Why gold behaves differently from stocks",
        "How trade tensions can move markets"],
    "company_analysis": [
        "How to read a balance sheet in plain terms", "Return on equity: what it tells you",
        "Why promoter holding is worth reading", "Earnings per share: what it measures",
        "Reading a quarterly result without the noise", "What operating margin shows about a business",
        "Book value versus market price", "Why revenue growth alone can mislead",
        "How to compare two companies in the same sector"],
    "ipo": [
        "What an anchor investor is in an IPO", "How IPO allotment works",
        "Retail, institutional and high-net-worth quotas in an IPO", "What lock-in periods mean after an IPO",
        "Fresh issue versus offer for sale", "What a red herring prospectus is",
        "Why valuation matters before you apply for an IPO", "What oversubscription means",
        "How a company gets listed on an exchange"],
    "technical_analysis": [
        "Moving averages: what they smooth out", "What trading volume adds to a chart",
        "RSI explained: what overbought and oversold mean", "Trend lines: how traders draw them and their limits",
        "Why indicators lag price", "Chart patterns: why they often fail",
        "Timeframes: why the same chart looks different", "Risk management comes before chart reading",
        "What backtesting can and cannot tell you"],
    "derivatives": [
        "What lot size means in futures and options", "How time decay works for option buyers",
        "What margin means in futures trading", "Hedging explained: options as insurance",
        "Strike price and moneyness in simple terms", "What implied volatility tells you",
        "Futures versus options: the key differences", "What happens on expiry day",
        "The risk profile of selling options"],
    "investing": [
        "Asset allocation: stocks, bonds and gold", "What risk tolerance really means",
        "Rebalancing a portfolio, explained", "Why starting early matters more than starting big",
        "Inflation and why cash loses value over time", "Rupee cost averaging in plain words",
        "How a goal timeline changes the way you invest", "Why costs and taxes quietly shape returns",
        "How to think about a long-term financial goal"],
    "mutual_funds_etfs": [
        "Active versus passive funds", "What a fund's exit load is", "Direct versus regular plans",
        "How ETFs trade differently from mutual funds", "What the riskometer on a fund shows",
        "Debt funds versus equity funds", "What tracking error means for index funds",
        "How to read a fund factsheet", "What a fund manager actually does"],
    "macro": [
        "What GDP growth means for investors", "What the repo rate is", "Why a falling rupee affects imports and markets",
        "What the fiscal deficit is", "Why inflation data moves markets", "What the yield curve says",
        "How rate changes reach your loan EMI", "What the current account deficit means",
        "Why the monsoon matters to the Indian economy"],
    "news": [
        "What a company's stock exchange filing is", "How to check where a news claim comes from",
        "What priced in means", "Why one headline rarely changes a long-term plan",
        "How analyst ratings and target prices work", "What bulk deals and block deals are",
        "How to read a market wrap", "What an earnings call reveals", "Why headline numbers need context"],
    "sectors": [
        "How the banking sector earns money", "What drives IT sector earnings", "How the pharma sector is different",
        "Why FMCG is called defensive", "How auto sales cycles work", "What a public sector company is",
        "Sector rotation explained", "How commodity prices affect metal and energy sectors",
        "Why real estate is sensitive to interest rates"],
    "history": [
        "The 1929 crash and the Great Depression in simple terms", "How SEBI came to regulate Indian markets",
        "The story of the Bombay Stock Exchange", "Black Monday of 1987", "The Dutch tulip mania",
        "What the collapse of Long-Term Capital Management taught markets", "The 2020 market crash and recovery",
        "How dematerialisation changed investing in India", "The story of the NSE and the Nifty"],
    "psychology_personal_finance": [
        "Loss aversion: why losses feel bigger than gains", "Why checking your portfolio daily can hurt",
        "FOMO and chasing last year's winners", "How to build a budget you can keep",
        "Needs versus wants in a monthly budget", "Why insurance comes before investing",
        "Anchoring: why your purchase price misleads you", "Overconfidence and why diversification protects you",
        "How to think about paying off debt versus investing"],
}
for _k, _titles in _MORE_EVERGREEN.items():
    EVERGREEN[_k] = EVERGREEN[_k] + _titles


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
