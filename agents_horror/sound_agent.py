# agents_horror/sound_agent.py
"""Stage 8 (sound design). Ambience bed + a few timed sound effects from Freesound, using the
helpers already written for the Bhakti pipeline (agents_bhakti.music_agent: search, download,
cache). CC0 first; CC-BY only if no CC0 match, and then attribution is returned so it goes into the
video description. No FREESOUND_API_KEY -> no sound design (narration + captions still render)."""
import os


def _music():
    from agents_bhakti import music_agent  # existing shared Freesound helpers
    return music_agent


def ambience_query(scenes, bible):
    """Bed query from the story's own atmosphere/weather, not a fixed track."""
    text = " ".join([bible.weather, bible.time_of_day, bible.palette] +
                    [s.ambience for s in scenes if s.ambience]).lower()
    for key, query in (("rain", "rain night ambience"), ("storm", "storm wind ambience"),
                       ("wind", "wind howling ambience"), ("forest", "forest night ambience"),
                       ("fog", "foggy night ambience"), ("corridor", "empty corridor ambience"),
                       ("hostel", "empty building ambience"), ("station", "empty station ambience")):
        if key in text:
            return query
    return "dark ambience drone"


def plan_sfx(scenes, max_sfx, intensity=3, min_gap=6.0, start_guard=2.0):
    """Pure function: which sfx cues, at what time. Never in the first seconds, never closer than
    `min_gap`, capped (jump-scare overload is a quality failure, not a feature)."""
    cap = min(max_sfx, 2 + intensity)
    cues, last = [], -999.0
    for sc in scenes:
        for q in sc.sfx[:1]:
            at = round(sc.start + 0.4, 2)
            if at < start_guard or at - last < min_gap or len(cues) >= cap:
                continue
            cues.append({"scene_id": sc.scene_id, "at": at, "query": q})
            last = at
    return cues


def fetch_audio(query, min_duration, search=None, download=None, key=None):
    """Returns {"path","license","name","author","url"} or None. CC0 -> CC-BY."""
    m = _music()
    api_key = key if key is not None else m.FREESOUND_API_KEY
    if not api_key:
        return None
    search = search or m._search_one
    download = download or m._download
    index = m._load_cache_index()
    for lic in (m.CC0, m.CC_BY):
        ck = m._cache_key("horror:" + query, lic)
        if ck in index and os.path.exists(index[ck]["path"]):
            return index[ck]
        try:
            results = search(query, lic, min_duration)
        except Exception as e:
            print(f"[horror sound] search failed '{query}': {e}")
            continue
        for track in results[:2]:
            try:
                path = download(track, ck)
            except Exception as e:
                print(f"[horror sound] download failed: {e}")
                continue
            if path:
                rec = {"path": path, "license": lic, "name": track.get("name", "Unknown"),
                       "author": track.get("username", "Unknown"), "url": track.get("url", "")}
                index[ck] = rec
                m._save_cache_index(index)
                return rec
    return None


def build_sound_design(cfg, scenes, bible, total_sec, fetch=None):
    """Returns {"bed": rec|None, "sfx": [{"at","path","scene_id","query"}], "attributions": [...], "notes": [...]}."""
    out = {"bed": None, "sfx": [], "attributions": [], "notes": []}
    if not cfg.music:
        out["notes"].append("sound design disabled (HORROR_MUSIC=0)")
        return out
    fetch = fetch or fetch_audio
    bed = fetch(ambience_query(scenes, bible), min(20.0, max(total_sec, 10.0)))
    if bed:
        out["bed"] = bed
    else:
        out["notes"].append("no ambience bed available (FREESOUND_API_KEY missing or nothing found)")
    for cue in plan_sfx(scenes, cfg.max_sfx, cfg.intensity):
        rec = fetch(cue["query"], 0.5)
        if rec:
            out["sfx"].append({**cue, "path": rec["path"]})
            if rec["license"] != "Creative Commons 0":
                out["attributions"].append(rec)
        else:
            out["notes"].append(f"no sfx found for '{cue['query']}' ({cue['scene_id']})")
    if bed and bed["license"] != "Creative Commons 0":
        out["attributions"].append(bed)
    return out


def attribution_lines(attributions):
    seen, lines = set(), []
    for a in attributions:
        key = a.get("url") or a.get("name")
        if key in seen:
            continue
        seen.add(key)
        lines.append(f'Sound: "{a.get("name")}" by {a.get("author")} (CC BY) {a.get("url", "")}'.strip())
    return lines
