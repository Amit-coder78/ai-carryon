"""Stage 6: deterministic fact-check gate. Errors block publication; warnings are logged."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .facts import (PLACEHOLDER, UnknownFact, facts_by_id, referenced_ids, render_template, ungrounded_numbers)

BANNED = [re.compile(p, re.I) for p in (
    r"\bguarantee[sd]?\b", r"\bsure[- ]?shot\b", r"\brisk[- ]?free\b", r"\bno risk\b",
    r"\bbuy (now|today|this)\b", r"\bsell (now|today|this)\b", r"\bwill (definitely|certainly|surely)\b",
    r"\bcan'?t lose\b", r"\bdouble your money\b", r"\bget rich\b", r"\bassured returns?\b",
    r"\b100% (returns?|profit|accura\w+|safe)\b", r"\bnext (big )?multibagger\b")]
NEGATED_GUARANTEE = re.compile(r"\b(?:no|not|never|without)\s+(?:a\s+)?guarantee[sd]?\b|n't\s+guarantee\b", re.I)
RISK_WORDS = re.compile(r"\b(risk|risks|risky|loss|losses|lose|volatile|volatility)\b", re.I)
HYPO_MARKERS = re.compile(r"\b(hypothetical|for example|for instance|assume|suppose|imagine|illustration|example)\b", re.I)
TICKER = re.compile(r"[A-Z0-9&\-\.\^]{1,20}")


@dataclass
class Finding:
    level: str   # error | warn
    code: str
    message: str


@dataclass
class Report:
    findings: list = field(default_factory=list)

    def add(self, level, code, msg): self.findings.append(Finding(level, code, msg))
    @property
    def errors(self): return [f for f in self.findings if f.level == "error"]
    @property
    def warnings(self): return [f for f in self.findings if f.level == "warn"]
    @property
    def passed(self): return not self.errors
    def summary(self): return "; ".join(f"{f.code}: {f.message}" for f in self.findings) or "ok"


def find_banned(text):
    cleaned = NEGATED_GUARANTEE.sub(" ", text)
    return [m.group(0) for p in BANNED for m in p.finditer(cleaned)]


def check_script(template, facts, settings, *, requires_current_data=False, allowed_literals=(), now=None):
    now = now or datetime.now(timezone.utc)
    rep = Report()
    fmap = facts_by_id(facts)

    try:
        rendered = render_template(template, fmap)
    except UnknownFact as e:
        rep.add("error", "UNKNOWN_FACT", f"placeholder references unknown fact {e}")
        return rep

    for tok in ungrounded_numbers(template, allowed_literals):
        rep.add("error", "UNGROUNDED_NUMBER", f"number {tok!r} typed by the model is not backed by a fact")
    for hit in find_banned(rendered):
        rep.add("error", "BANNED_PHRASE", f"promotional/guarantee language: {hit!r}")

    used = [fmap[i] for i in dict.fromkeys(referenced_ids(template))]
    for f in used:
        as_of = f.as_of_dt()
        if as_of and as_of > now + timedelta(days=1):
            rep.add("error", "FUTURE_DATE", f"{f.id} has as_of in the future")
        if f.kind == "current":
            if not (f.source_url and as_of):
                rep.add("error", "UNSOURCED_CURRENT", f"{f.id} needs source_url and as_of")
            elif now - as_of > timedelta(hours=settings.data_max_age_hours):
                age_h = (now - as_of).total_seconds() / 3600
                rep.add("error", "STALE_DATA", f"{f.id} is {age_h:.0f}h old (max {settings.data_max_age_hours}h)")
        elif f.kind == "historical":
            if not f.source_url: rep.add("error", "UNSOURCED_HISTORICAL", f"{f.id} needs source_url")
        elif f.kind == "forecast":
            if not (f.attribution and f.source_url):
                rep.add("error", "UNATTRIBUTED_FORECAST", f"{f.id} must name who forecast it and cite a source")
        elif f.kind == "hypothetical":
            if not f.note: rep.add("error", "UNLABELED_HYPOTHETICAL", f"{f.id} needs note stating its assumptions")
            if not HYPO_MARKERS.search(rendered):
                rep.add("error", "HYPOTHETICAL_NOT_SIGNPOSTED", "script uses a hypothetical figure without saying it is an example")
        elif f.kind == "computed":
            missing = [d for d in f.derived_from if d not in fmap]
            if not f.derived_from or missing:
                rep.add("error", "BAD_DERIVATION", f"{f.id} derived_from missing/unknown: {missing or 'empty'}")
        elif f.kind != "concept":
            rep.add("error", "BAD_KIND", f"{f.id} has unknown kind {f.kind!r}")

    seen = {}
    for f in fmap.values():
        key = (f.label.lower(), f.kind)
        if key in seen and seen[key] != f.value:
            rep.add("error", "CONTRADICTION", f"conflicting values for {f.label!r}")
        seen[key] = f.value

    if requires_current_data and not any(f.kind in ("current", "computed") and
                                         (f.kind == "current" or all(fmap[d].kind == "current" for d in f.derived_from if d in fmap))
                                         for f in used):
        rep.add("error", "NO_VERIFIED_CURRENT_DATA", "current-market content needs at least one verified, fresh data point")

    if settings.cat.needs_risk_statement and not RISK_WORDS.search(rendered):
        rep.add("error", "MISSING_RISK", f"{settings.category} content must state the risk of loss")

    if settings.ticker and not TICKER.fullmatch(settings.ticker):
        rep.add("error", "BAD_TICKER", f"invalid ticker {settings.ticker!r}")

    words = len(rendered.split())
    target = settings.duration_sec * settings.words_per_second
    if not 0.75 * target <= words <= 1.25 * target:
        rep.add("warn", "LENGTH", f"{words} words vs target ~{target:.0f}")
    return rep
