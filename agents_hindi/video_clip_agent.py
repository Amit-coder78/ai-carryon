# agents_hindi/video_clip_agent.py
"""
Hindi V2 — Step 2: smarter Pexels footage.

Old behaviour (agents/video_clip_agent.py, shared with English):
  photo-style prompt ("A teenage gamer hunched over a laptop ... DSLR 4K")
  -> first 5 words -> first Pexels result.
That is why a WiFi video got gamers and couples.

New behaviour:
  1. The LLM splits the narration into N parts and writes 3 SHORT stock-footage
     queries per part (objects/close-ups, not people acting).
  2. Each scene searches Pexels with those queries and pools the candidates.
  3. Candidates are ranked (title match, portrait, resolution, length).
  4. Clips used in earlier videos (remembered in the shared DB) are skipped.

Same contract as the old function: generate_background_clips(topic, script,
num_clips) -> (clip_paths, errors), files saved as assets/pexels_clips/{i}.mp4.
"""
import json
import os
import re

import requests

from agents_hindi.categories import classify_topic

PEXELS_VIDEO_SEARCH = "https://api.pexels.com/videos/search"
CLIP_FOLDER = "assets/pexels_clips"
USED_IDS_KEY = "hindi_used_pexels_ids"
MAX_REMEMBERED = 300          # how many past clip ids to remember
WORDS_PER_SECOND = 2.7        # measured: 88 words ~ 32s of Sarvam voice

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "vs", "kya", "kaise",
    "kyun", "yeh", "ye", "hai", "hain", "ki", "ka", "ke", "ko", "se", "mein",
    "toh", "ek", "aur", "par", "kahani", "dekho", "karo", "jab", "agar",
}

_GENERIC_QUERIES = {
    "science": ["science experiment close up", "laboratory liquid reaction", "chemistry beaker"],
    "ai": ["artificial intelligence screen", "computer code screen", "robot arm"],
    "tech": ["circuit board close up", "electronics workbench", "server room lights"],
    "gadgets": ["smartphone close up", "gadget on table", "hands holding phone"],
}


# ─────────────────────────────────────────────
# Remembering used clips (shared DB, survives Cloud Run restarts)
# ─────────────────────────────────────────────

def _load_used_ids():
    try:
        from agents.database import db
        raw = db.get_meta(USED_IDS_KEY)
        return [int(x) for x in json.loads(raw)] if raw else []
    except Exception as e:
        print(f"Used-clip memory unavailable (load): {e}")
        return []


def _save_used_ids(ids):
    try:
        from agents.database import db
        db.set_meta(USED_IDS_KEY, json.dumps(ids[-MAX_REMEMBERED:]))
    except Exception as e:
        print(f"Used-clip memory unavailable (save): {e}")


# ─────────────────────────────────────────────
# 1. Scene queries
# ─────────────────────────────────────────────

def _clean_query(q):
    q = re.sub(r"[^a-zA-Z0-9 ]", " ", str(q)).lower()
    words = [w for w in q.split() if w]
    return " ".join(words[:4])


def _fallback_queries(topic, category, num_scenes):
    """Used when the LLM fails: topic keywords + generic category footage."""
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", topic.lower())
             if len(w) > 2 and w not in _STOPWORDS][:3]
    topic_q = " ".join(words)
    generic = _GENERIC_QUERIES.get(category or "science", _GENERIC_QUERIES["science"])
    scenes = []
    for i in range(num_scenes):
        qs = []
        if topic_q:
            qs.append(topic_q)
        qs.append(generic[i % len(generic)])
        qs.append(generic[(i + 1) % len(generic)])
        scenes.append(qs)
    return scenes


def plan_scene_queries(topic, script, num_scenes, category=None):
    """Return a list of `num_scenes` lists of search queries."""
    try:
        from agents_hindi.model_invoke_agent_hindi import safe_invoke
        prompt = f"""You are a video editor choosing STOCK FOOTAGE from Pexels for a {num_scenes}-scene vertical YouTube Short.

Topic: {topic}
Narration (Hinglish): {script}

Split the narration into {num_scenes} consecutive parts, in order. For each part give 3 Pexels search queries, from most specific to most general.

Rules:
- English only, 2-4 words per query.
- Simple concrete things a camera can film: objects, close-ups, hands, machines, screens, lab equipment, nature, city.
- Show the THING being talked about, not people's emotions. Good: "wifi router close up", "router blinking lights", "dry ice smoke", "liquid foam reaction", "smartphone water splash".
- No brand names, no text or logos, no news events, no abstract ideas (like "success" or "mystery").
- The 3 queries of one part must be different ideas, not just synonyms.

Return ONLY JSON: [{{"scene": 1, "queries": ["...", "...", "..."]}}]"""
        content = (safe_invoke(prompt, temperature=0.4).content or "").strip()
        start, end = content.find("["), content.rfind("]")
        data = json.loads(content[start:end + 1])
        scenes = []
        for item in data:
            qs = [_clean_query(q) for q in item.get("queries", [])]
            qs = [q for q in qs if q]
            if qs:
                scenes.append(qs[:3])
        if len(scenes) >= num_scenes:
            return scenes[:num_scenes]
        raise ValueError(f"only {len(scenes)} usable scenes")
    except Exception as e:
        print(f"Scene query planning failed ({e}) — using fallback queries")
        return _fallback_queries(topic, category, num_scenes)


# ─────────────────────────────────────────────
# 2. Search
# ─────────────────────────────────────────────

def search_candidates(query, per_page=15, orientation="portrait"):
    key = os.getenv("PEXELS_API_KEY", "")
    if not key:
        return []
    params = {"query": query, "per_page": per_page}
    if orientation:
        params["orientation"] = orientation
    try:
        r = requests.get(PEXELS_VIDEO_SEARCH, headers={"Authorization": key},
                         params=params, timeout=30)
        r.raise_for_status()
        videos = r.json().get("videos", [])
    except Exception as e:
        print(f"  Pexels search failed for '{query}': {e}")
        return []
    return [{
        "id": v.get("id"),
        "width": v.get("width") or 0,
        "height": v.get("height") or 0,
        "duration": v.get("duration") or 0,
        "url": v.get("url", ""),
        "files": v.get("video_files", []),
        "query": query,
    } for v in videos if v.get("id")]


# ─────────────────────────────────────────────
# 3. Ranking
# ─────────────────────────────────────────────

def _slug_words(url):
    """Pexels page urls end with a readable title, e.g.
    .../video/a-wifi-router-on-a-table-3045678/ -> {a, wifi, router, on, table}"""
    m = re.search(r"/video/([^/]+)/?$", url or "")
    if not m:
        return set()
    return {w for w in m.group(1).split("-") if not w.isdigit()}


def score_candidate(c, query_rank, target_sec, channel="hindi"):
    q_words = set(c["query"].split())
    relevance = len(q_words & _slug_words(c["url"])) / max(len(q_words), 1)

    penalty = 0.0
    try:  # shared semantic scorer; any problem keeps the old relevance above
        from agents import clip_ranker
        if clip_ranker.enabled(channel):
            relevance, rejected = clip_ranker.score_title(
                c["query"], clip_ranker.slug_title(c["url"]),
                clip_ranker.CHANNEL_REJECT.get(channel, ()))
            if rejected:  # wrong meaning (e.g. "car driver" for "screwdriver"): push to the bottom
                penalty = 5.0
    except Exception:
        pass

    score = relevance * 4.0 - penalty
    score += (1.0, 0.5, 0.25)[min(query_rank, 2)]          # earlier query = more specific

    portrait = c["height"] > c["width"]
    score += 1.0 if portrait else 0.0

    short_side = c["width"] if portrait else c["height"]
    score += 1.0 if short_side >= 1080 else 0.5 if short_side >= 720 else -1.0

    d = c["duration"]
    if target_sec * 0.9 <= d <= 30:
        score += 1.5                                        # long enough, no looping
    elif d >= target_sec * 0.5:
        score += 0.5
    else:
        score -= 0.5                                        # will loop a lot
    if d > 45:
        score -= 0.5
    return score


def rank_candidates(per_query_results, banned_ids, target_sec, channel="hindi"):
    """per_query_results: list of candidate lists, one per query (in order)."""
    best = {}
    for rank, results in enumerate(per_query_results):
        for c in results:
            if c["id"] in banned_ids:
                continue
            s = score_candidate(c, rank, target_sec, channel)
            if c["id"] not in best or s > best[c["id"]][0]:
                best[c["id"]] = (s, c)
    return [c for _, c in sorted(best.values(), key=lambda x: -x[0])]


# ─────────────────────────────────────────────
# 4. Download + main
# ─────────────────────────────────────────────

def _pick_best_video_file(video_files):
    """Pexels gives several encodes per clip; take the mp4 closest to 1080px wide."""
    if not video_files:
        return None
    mp4 = [f for f in video_files if f.get("file_type") == "video/mp4"] or video_files
    mp4.sort(key=lambda f: abs((f.get("width") or 0) - 1080))
    return mp4[0].get("link")


def _download(candidate, output_path):
    link = _pick_best_video_file(candidate["files"])
    if not link:
        raise RuntimeError("no downloadable file")
    resp = requests.get(link, timeout=90, stream=True)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)


def generate_background_clips(topic, script, num_clips=4):
    os.makedirs(CLIP_FOLDER, exist_ok=True)
    for f in os.listdir(CLIP_FOLDER):
        os.remove(os.path.join(CLIP_FOLDER, f))

    clean_topic = topic.split("||PATTERN:")[0].strip()
    category = classify_topic(clean_topic)
    est_total = max(len(script.split()) / WORDS_PER_SECOND, 10)
    target_sec = est_total / num_clips

    scenes = plan_scene_queries(clean_topic, script, num_clips, category)

    remembered = _load_used_ids()
    banned = set(remembered)
    new_ids = []
    clip_paths, errors = [], []

    print(f"\nPexels clips for: {clean_topic}  (~{target_sec:.0f}s per clip)")
    for i, queries in enumerate(scenes):
        print(f"  Scene {i + 1}: {queries}")
        results = []
        for qi, q in enumerate(queries):
            results.append(search_candidates(q))
            # always use the first 2 queries; use the 3rd only if the pool is thin
            enough = sum(len(r) for r in results) >= 6
            if qi >= 1 and enough:
                break
        ranked = rank_candidates(results, banned, target_sec)

        if not ranked:  # nothing portrait — retry broad, any orientation
            print("    no portrait results — trying any orientation")
            ranked = rank_candidates([search_candidates(queries[0], orientation=None)],
                                     banned, target_sec)

        out = os.path.join(CLIP_FOLDER, f"{i + 1}.mp4")
        done = False
        for cand in ranked[:3]:
            try:
                _download(cand, out)
                clip_paths.append(out)
                banned.add(cand["id"])
                new_ids.append(cand["id"])
                print(f"    Clip {i + 1}: id={cand['id']} ({cand['duration']}s) "
                      f"via '{cand['query']}'")
                done = True
                break
            except Exception as e:
                print(f"    download failed for id={cand['id']}: {e}")
        if not done:
            errors.append(f"Scene {i + 1}: no usable clip for {queries}")

    if new_ids:
        _save_used_ids(remembered + new_ids)
    return clip_paths, errors
