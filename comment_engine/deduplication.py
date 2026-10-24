"""Comment and reply de-duplication."""
import re
import unicodedata
from difflib import SequenceMatcher

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text):
    t = unicodedata.normalize("NFKC", text or "").lower()
    return _WS.sub(" ", _PUNCT.sub(" ", t)).strip()


def similarity(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(jaccard, SequenceMatcher(None, na, nb).ratio())


def is_duplicate_reply(text, recent_replies, threshold=0.88):
    """True if `text` (nearly) repeats any of the recent replies for this channel."""
    return any(similarity(text, r) >= threshold for r in recent_replies if r)


def dedupe_comments(items, known_ids=()):
    """Drop repeated provider ids (within the batch and vs known ids) and flag
    the same author posting identical text repeatedly (returns (kept, repeats))."""
    seen, kept, repeats = set(known_ids), [], []
    by_author_text = {}
    for it in items:
        cid = it["comment_id"]
        if cid in seen:
            continue
        seen.add(cid)
        key = (it.get("author_channel_id") or it.get("author_name") or "", normalize(it.get("text", "")))
        if key[0] and key[1] and key in by_author_text:
            repeats.append(it)
            continue
        by_author_text[key] = cid
        kept.append(it)
    return kept, repeats
