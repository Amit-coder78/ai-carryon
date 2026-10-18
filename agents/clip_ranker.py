# agents/clip_ranker.py
"""
Shared Pexels clip ranking for the English, Hindi, Cricket and Bhakti pipelines.

Zero API calls, zero GPU: it only reads the metadata Pexels already returns
(the clip title hidden in the page URL slug, size, duration).

Why it exists: Pexels search is keyword-based, so a query for "screwdriver" can
return a clip titled "car driver". The old English/Bhakti path asked Pexels for
exactly 1 result and accepted it blindly. Now we ask for a pool of candidates
and keep only clips whose title really matches the query.

Honest limitation: this is lexical matching (stemming, compound words like
"screw driver" == "screwdriver", reject words), not real embeddings. It cannot
know that "wrench" and "spanner" are synonyms. Add embeddings later only if
logs show that matters.

Kill switches:
    SEMANTIC_CLIPS=0                 turns it off everywhere (old behaviour)
    SEMANTIC_CLIPS_<CHANNEL>=0       turns it off for one channel
                                     (ENGLISH, HINDI, CRICKET, BHAKTI)

Contract: callers wrap every use in try/except and fall back to the old
behaviour, so a bug here can never stop a video from rendering.
"""
import os
import re

# Words that carry no subject.
_FILLER = {"the", "a", "an", "of", "in", "on", "at", "to", "and", "with", "for", "from",
           "by", "is", "are", "this", "that", "over", "into", "near"}
# Words that describe the *shot*, not the subject (do not count as topical match).
_STYLE = {"close", "up", "closeup", "slow", "motion", "cinematic", "footage", "video",
          "clip", "shot", "view", "background", "hd", "4k", "roll"}

# Two kinds of mix-up. Extend both lists whenever you spot one in the logs.
#
# SOUNDALIKE: the subject word is unambiguous, but Pexels returns a similar-looking
# word. A reject word only counts when the subject word is missing from the title, so
# "screwdriver repairing a car" is kept but "car driver" is dropped.
SOUNDALIKE = {
    "screwdriver": ("car", "cars", "taxi", "bus", "truck", "racing", "race", "uber",
                    "driving", "license"),
}
# AMBIGUOUS: the subject word itself has two meanings, so the title can contain it and
# still be wrong ("cricket insect on leaf"). Reject words always count here.
AMBIGUOUS = {
    "python": ("snake", "snakes", "reptile"),
    "cricket": ("insect", "insects", "grasshopper", "bug", "chirping", "cicada"),
}

# Per-channel words that must never appear in a clip title (unless the query itself uses them).
CHANNEL_REJECT = {
    "cricket": ("football", "soccer", "basketball", "tennis", "baseball"),
}


def enabled(channel="english"):
    if os.getenv("SEMANTIC_CLIPS", "1").strip() == "0":
        return False
    return os.getenv(f"SEMANTIC_CLIPS_{str(channel).upper()}", "1").strip() != "0"


def _tokens(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def stem(w):
    """Tiny suffix stripper. Applied to both sides, so it only has to be consistent."""
    w = (w or "").lower()
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def slug_title(url):
    """https://www.pexels.com/video/a-wifi-router-3045678/ -> 'a wifi router'"""
    m = re.search(r"/video/([^/]+)/?$", url or "")
    if not m:
        return ""
    return " ".join(w for w in m.group(1).split("-") if not w.isdigit())


def _query_words(query):
    out, seen = [], set()
    for w in _tokens(query):
        if w in _FILLER or w in _STYLE or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def compact_query(text, max_words=5):
    """Drop filler words BEFORE trimming, so the noun is not cut off by the word limit."""
    words = [w for w in _tokens(text) if w not in _FILLER]
    return " ".join(words[:max_words])


def broaden_query(text):
    """Retry query: the two longest subject words, in their original order.
    (The old retry kept the first 2 words, which could be just 'close up'.)"""
    words = _query_words(text)
    if not words:
        return ""
    keep = set(sorted(range(len(words)), key=lambda i: -len(words[i]))[:2])
    return " ".join(w for i, w in enumerate(words) if i in keep)


def score_title(query, title, extra_reject=()):
    """Return (relevance 0-1, rejected). relevance = share of the query's subject
    words found in the title. rejected = title shows the wrong meaning."""
    qw = _query_words(query)
    if not qw:
        return 0.0, False
    tw = _tokens(title)
    t_stems = {stem(w) for w in tw}
    joined = {stem(a + b) for a, b in zip(tw, tw[1:])}          # "screw driver" -> screwdriver
    have = t_stems | joined

    hit = {i for i, w in enumerate(qw) if stem(w) in have}
    for i in range(len(qw) - 1):                                # "wi fi" in query -> wifi in title
        if stem(qw[i] + qw[i + 1]) in have:
            hit.update((i, i + 1))
    relevance = len(hit) / len(qw)

    q_stems = {stem(w) for w in qw}
    reject = {stem(w) for w in extra_reject}
    for key, words in SOUNDALIKE.items():
        if stem(key) in q_stems and stem(key) not in have:
            reject |= {stem(w) for w in words}
    for key, words in AMBIGUOUS.items():
        if stem(key) in q_stems:
            reject |= {stem(w) for w in words}
    rejected = bool(t_stems & (reject - q_stems))
    return relevance, rejected


def _quality(c, min_duration, max_duration):
    """0..1.5 from metadata only (portrait, resolution, duration window)."""
    w, h = c.get("width") or 0, c.get("height") or 0
    short = min(w, h) if w and h else 0
    q = 0.5 if h > w else 0.0
    q += 0.5 if short >= 1080 else 0.25 if short >= 720 else 0.0
    d = c.get("duration") or 0
    lo = min_duration or 0
    hi = max_duration or 10 ** 6
    q += 0.5 if lo <= d <= hi else 0.0
    return q


def rank_for_query(query, candidates, min_duration=None, max_duration=None,
                   exclude_ids=None, extra_reject=(), require_match=True):
    """Rank normalized Pexels candidates ({"id","url","width","height","duration","files"}).
    Returns a best-first list (copies with "relevance"/"score" added). With
    require_match=True a clip whose title shares no subject word with the query,
    or shows the wrong meaning, is dropped, so [] means "nothing relevant here"."""
    exclude = {str(x) for x in (exclude_ids or [])}
    n = max(len(candidates), 1)
    scored = []
    for i, c in enumerate(candidates):
        if str(c.get("id")) in exclude:
            continue
        d = c.get("duration") or 0
        if d and ((min_duration and d < min_duration) or (max_duration and d > max_duration)):
            continue
        rel, rejected = score_title(query, slug_title(c.get("url", "")), extra_reject)
        if rejected or (require_match and rel <= 0):
            continue
        order_bonus = 0.5 * (1 - i / n)                          # Pexels' own order breaks ties
        score = rel * 5.0 + order_bonus + _quality(c, min_duration, max_duration)
        scored.append(dict(c, relevance=rel, score=score))
    scored.sort(key=lambda c: -c["score"])
    if scored:
        top = scored[0]
        print(f"[clip_ranker] '{query}' -> id={top['id']} rel={top['relevance']:.2f} "
              f"title='{slug_title(top.get('url', ''))}' ({len(scored)}/{len(candidates)} passed)")
    else:
        print(f"[clip_ranker] '{query}' -> no relevant clip among {len(candidates)}")
    return scored
