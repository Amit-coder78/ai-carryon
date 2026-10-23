# agents_horror/series.py
"""Series continuity + anti-repetition memory, stored in the shared DB meta table
(agents.database.db.get_meta / set_meta), so it survives GitHub Actions' empty disk."""
import json

RECENT_KEY = "horror_recent"
MAX_RECENT = 30
MAX_SUMMARIES = 10


def _load(meta_get, key, default):
    try:
        raw = meta_get(key)
        return json.loads(raw) if raw else default
    except Exception:
        return default


def load_recent(meta_get):
    d = _load(meta_get, RECENT_KEY, {})
    return {"categories": list(d.get("categories", [])), "premises": list(d.get("premises", [])),
            "settings": list(d.get("settings", []))}


def record_recent(meta_set, meta_get, category, premise, setting=""):
    d = load_recent(meta_get)
    d["categories"] = (d["categories"] + [category])[-MAX_RECENT:]
    d["premises"] = (d["premises"] + [premise])[-MAX_RECENT:]
    d["settings"] = (d["settings"] + [setting])[-MAX_RECENT:]
    meta_set(RECENT_KEY, json.dumps(d))


def _series_key(series_id):
    return f"horror_series:{series_id}"


def load_series(meta_get, series_id):
    d = _load(meta_get, _series_key(series_id), {})
    return {"series_id": series_id, "title": d.get("title", ""), "episode": int(d.get("episode", 0)),
            "summaries": list(d.get("summaries", [])), "bible": d.get("bible") or None}


def save_episode(meta_get, meta_set, series_id, title, episode, summary, bible_dict):
    cur = load_series(meta_get, series_id)
    summaries = (cur["summaries"] + [{"episode": episode, "summary": summary}])[-MAX_SUMMARIES:]
    meta_set(_series_key(series_id), json.dumps({
        "title": title or cur["title"], "episode": episode, "summaries": summaries, "bible": bible_dict}))


def word_set(text):
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in (text or "")).split() if len(w) > 3}


def similarity(a, b):
    """Jaccard overlap of content words: cheap premise-repetition check."""
    wa, wb = word_set(a), word_set(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
