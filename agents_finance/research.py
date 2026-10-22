"""Stage 2: research. Pulls items from RSS/Atom feeds listed in FINANCE_FEEDS (JSON). No default feeds are
shipped: prefer official sources (exchange/RBI/SEBI/central-bank feeds) and verify each URL from your runner."""
import json, os, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .sources import SourceRecord, UNRATED


def _default_fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-carryon-finance/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _parse_date(s):
    if not s: return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try: d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError: return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml_text, publisher=""):
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"): continue
        g = lambda n: next((c for c in it if c.tag.split("}")[-1] == n), None)
        title = (g("title").text or "").strip() if g("title") is not None else ""
        link_el = g("link")
        link = (link_el.get("href") or link_el.text or "").strip() if link_el is not None else ""
        date_el = next((e for e in (g("pubDate"), g("published"), g("updated")) if e is not None), None)  # Element truthiness is child-count, so test `is not None`
        d = _parse_date(date_el.text if date_el is not None else "")
        if title and link and d:
            out.append({"title": title, "url": link, "published": d, "publisher": publisher})
    return out


def feeds_from_env(env=None):
    raw = (env or os.environ).get("FINANCE_FEEDS", "")
    return json.loads(raw) if raw else []


def gather(settings, feeds, fetch=_default_fetch, now=None):
    """Returns (items, errors). Items are fresh, sourced, tiered; unrated sources are dropped for news."""
    now = now or datetime.now(timezone.utc)
    items, errors = [], []
    for f in feeds:
        try:
            for e in parse_feed(fetch(f["url"]), f.get("publisher", "")):
                age = (now - e["published"]).total_seconds() / 3600
                if age > settings.research_max_age_hours or age < -1: continue
                rec = SourceRecord(url=e["url"], title=e["title"], publisher=e["publisher"],
                                   published_at=e["published"].isoformat(), tier=int(f.get("tier", UNRATED)) if f.get("tier") else UNRATED)
                if rec.tier == UNRATED: continue
                items.append({"title": e["title"], "source": rec.to_dict()})
        except Exception as ex:
            errors.append(f"{f.get('url')}: {ex}")
    items.sort(key=lambda i: (i["source"]["tier"], -datetime.fromisoformat(i["source"]["published_at"]).timestamp()))
    return items, errors
