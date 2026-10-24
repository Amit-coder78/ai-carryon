"""
topic_selection.py — Brain-ranked topic choice for a channel's scheduler.

brain_ranked_topic() returns a topic the Brain preferred, or None. None means "use the existing
selection unchanged" — and it is returned for every non-happy path (feature off, engine down or
uninformative, nothing eligible, every ranked topic rejected), so a channel without a Brain
behaves exactly as it did before this module existed. When the feature is off it does no work at
all (no YouTube call, no DB read).

Existing safeguards still apply to the Brain's choice: topics posted in the last 24h are dropped
before ranking, and each ranked topic still has to pass the saturation check, in Brain order.
Every Brain-assisted decision logs one `BRAIN_LOG {...}` line with the source, execution id,
policy version and Brain versions.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from agents import brain_client

DEMAND_LOOKBACK_DAYS = 30
OVERLAP_THRESHOLD = 0.6
_STOP = {"the", "and", "for", "with", "this", "that", "from", "are", "was", "new", "how", "what",
         "why", "you", "your", "its", "has", "have", "will", "can", "all", "not", "but", "out", "now"}


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if (len(w) >= 3 or w == "ai") and w not in _STOP}


def _similar(a, b):
    """Overlap coefficient: a short viewer request ('quantum computing') matches a long title."""
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= OVERLAP_THRESHOLD


def demand_features(candidates, requests, now=None):
    """candidates: [{"title", "age_days"}]; requests: topic_requests rows (dicts with topic, timestamp,
    author_channel_id). Returns {title: features}. `unique_requesters` counts DISTINCT known
    authors (rows without an author id never count as a person), `similar_requests` is the sample
    size. Candidates nobody asked for get only the freshness signal."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DEMAND_LOOKBACK_DAYS)
    recent = []
    for r in requests or []:
        try:
            ts = datetime.fromisoformat(str(r.get("timestamp")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts >= cutoff:
            recent.append((_words(r.get("topic")) or _words(r.get("comment")), r.get("author_channel_id")))

    out = {}
    for c in candidates:
        feats = {}
        if c.get("age_days") is not None:
            feats["days_since_trend"] = round(float(c["age_days"]), 3)
        cw = _words(c["title"])
        matches = [author for words, author in recent if _similar(words, cw)]
        if matches:
            feats["similar_requests"] = len(matches)
            feats["unique_requesters"] = len({a for a in matches if a})
        out[c["title"]] = feats
    return out


def brain_ranked_topic(channel, region_code="US", *, recent_topics=(), max_attempts=5, log=print,
                       get_candidates=None, get_requests=None, check_saturation=None,
                       mark_used=None, rank=None):
    """Returns the chosen topic str, or None to fall back. All collaborators are injectable for tests."""
    if not brain_client.is_configured(channel):
        return None

    if get_candidates is None:
        from agents.trending_agent import get_trending_candidates as get_candidates
    if get_requests is None:
        def get_requests():
            from agents.database import db
            return db.get_topic_requests(channel, limit=500)
    if check_saturation is None:
        from agents.saturation_agent import check_saturation
    if mark_used is None:
        from agents.trending_agent import mark_topic_used as mark_used
    rank = rank or brain_client.rank_topics

    candidates = [c for c in get_candidates(region_code) if c["title"].lower().strip() not in set(recent_topics)]
    if len(candidates) < 2:
        log("Brain: fewer than 2 eligible candidates — using existing topic selection")
        return None

    try:
        features = demand_features(candidates, get_requests())
    except Exception as e:                       # a signal hiccup must never block topic selection
        log(f"Brain: demand signals unavailable ({type(e).__name__}) — ranking on freshness only")
        features = {c["title"]: ({"days_since_trend": c["age_days"]} if c.get("age_days") is not None else {})
                    for c in candidates}

    result = rank([c["title"] for c in candidates], channel, features=features)
    log("BRAIN_LOG " + json.dumps(result.to_log_dict(), default=str))
    if not result.applied:
        log(f"Brain: not applied ({result.reason}) — using existing topic selection")
        return None

    for attempt, topic in enumerate(result.ordered[:max_attempts], start=1):
        saturation = check_saturation(topic)
        log(f"Saturation check: score={saturation['opportunity_score']} — {saturation['reason']}")
        if saturation["proceed"]:
            mark_used(topic)
            log(f"Brain-ranked topic chosen (rank {attempt}): {topic}")
            return topic
        log(f"Brain-ranked topic too saturated, trying next ({attempt}/{max_attempts}): {topic}")
    log("Brain: every ranked topic was saturated — using existing topic selection")
    return None
