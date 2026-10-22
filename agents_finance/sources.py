"""Source records and reliability tiers (1 = best)."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from urllib.parse import urlparse

TIER_RULES = (
    (1, ("nseindia.com", "bseindia.com", "sec.gov", "nasdaq.com", "nyse.com")),
    (2, ()),  # company investor-relations pages: set per-source via tier=2 when known
    (3, ("rbi.org.in", "sebi.gov.in", "federalreserve.gov", "ecb.europa.eu", "bankofengland.co.uk", "imf.org", "worldbank.org")),
    (4, ("mospi.gov.in", "data.gov.in", "bls.gov", "bea.gov", "fred.stlouisfed.org", "pib.gov.in", "indiabudget.gov.in")),
    (5, ("reuters.com", "bloomberg.com", "ft.com", "wsj.com", "economictimes.indiatimes.com", "livemint.com", "business-standard.com", "thehindubusinessline.com")),
)
UNRATED = 9


def host_of(url):
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def classify_tier(url):
    h = host_of(url)
    for tier, domains in TIER_RULES:
        if any(h == d or h.endswith("." + d) for d in domains):
            return tier
    if h.endswith(".gov") or h.endswith(".gov.in"):
        return 4
    return UNRATED


@dataclass
class SourceRecord:
    url: str
    title: str
    publisher: str = ""
    published_at: str = ""   # ISO
    data_as_of: str = ""     # ISO date the figures refer to
    claims: list = field(default_factory=list)
    tier: int = UNRATED

    def __post_init__(self):
        if self.tier == UNRATED: self.tier = classify_tier(self.url)

    def age_hours(self, now=None):
        if not self.published_at: return None
        now = now or datetime.now(timezone.utc)
        d = datetime.fromisoformat(self.published_at)
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return (now - d).total_seconds() / 3600.0

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, d): return cls(**d)
