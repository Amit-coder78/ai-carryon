# agents_horror/clip_agent.py
"""Stage 6/7: visual sourcing. Reuses the SAME Pexels integration as Cricket/Hindi:
agents_hindi.video_clip_agent (search_candidates / rank_candidates / _download) and the
shared agents.clip_ranker relevance scorer. Clips go to assets/horror_clips/ because the
other pipelines wipe assets/pexels_clips/ on every run.

Rules: a clip is only used if its Pexels title matches the scene query (relevance gate).
No match -> same-location clip already chosen for an earlier scene (flagged), else the scene
is flagged "missing". Unrelated footage is never inserted to fill the timeline."""
import json
import os

CLIP_FOLDER = "assets/horror_clips"
USED_IDS_KEY = "horror_used_pexels_ids"
MAX_REMEMBERED = 300
REJECT_WORDS = ("cartoon", "halloween party", "costume", "pumpkin patch", "funny", "cute", "kids", "wedding")


def _hindi():
    from agents_hindi import video_clip_agent as h  # existing shared Pexels helpers
    return h


def default_search(query, orientation="portrait"):
    return _hindi().search_candidates(query, orientation=orientation)


def default_rank(pools, banned, target_sec):
    return _hindi().rank_candidates(pools, banned, target_sec, channel="horror")


def default_download(cand, path):
    return _hindi()._download(cand, path)


def relevance(query, cand):
    """(relevance 0-1, rejected) of a candidate against the query it was found with."""
    try:
        from agents import clip_ranker
        return clip_ranker.score_title(query, clip_ranker.slug_title(cand.get("url", "")), REJECT_WORDS)
    except Exception:
        words = {w for w in query.lower().split() if len(w) > 2}
        title = set((cand.get("url", "").rsplit("/video/", 1)[-1]).lower().replace("-", " ").split())
        rejected = bool(title & set(REJECT_WORDS))
        return (len(words & title) / len(words) if words else 0.0), rejected


def load_used_ids(meta_get):
    try:
        raw = meta_get(USED_IDS_KEY)
        return [int(x) for x in json.loads(raw)] if raw else []
    except Exception:
        return []


def commit_used_ids(meta_get, meta_set, assets):
    """Call only AFTER a successful render, so a failed run does not burn clips."""
    ids = [a["pexels_id"] for a in assets.values() if a.get("status") == "ok" and a.get("pexels_id")]
    if ids:
        meta_set(USED_IDS_KEY, json.dumps((load_used_ids(meta_get) + ids)[-MAX_REMEMBERED:]))


def _queries_for(scene, bible, broaden=False):
    from agents_horror.scene_planner import default_keywords, clean_keyword
    qs = [clean_keyword(k) for k in scene.search_keywords if clean_keyword(k)]
    if broaden or len(qs) < 2:
        qs += default_keywords(scene, bible)
    return list(dict.fromkeys(qs))[:4]


def _source_scene(scene, bible, cfg, banned, folder, search, rank, download, broaden=False):
    queries = _queries_for(scene, bible, broaden)
    target = max(scene.duration, 3.0)
    pools = []
    for qi, q in enumerate(queries):
        pools.append(search(q, "portrait") if not broaden else search(q, None))
        if qi >= 1 and sum(len(p) for p in pools) >= 6:
            break
    ranked = rank(pools, banned, target)
    out = os.path.join(folder, f"{scene.scene_id}.mp4")
    tried = 0
    for cand in ranked:
        rel, rejected = relevance(cand.get("query", queries[0]), cand)
        if rejected or rel < cfg.min_relevance:
            continue
        if tried >= 3:
            break
        tried += 1
        try:
            download(cand, out)
        except Exception as e:
            print(f"  [horror] {scene.scene_id}: download failed for id={cand['id']}: {e}")
            continue
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            continue
        banned.add(cand["id"])
        return {"scene_id": scene.scene_id, "status": "ok", "path": out, "pexels_id": cand["id"],
                "query": cand.get("query", queries[0]), "relevance": round(rel, 2),
                "source": "pexels", "note": ""}
    return None


def source_clips(scenes, bible, cfg, meta_get, only=None, existing=None, broaden=False,
                 folder=CLIP_FOLDER, search=None, rank=None, download=None):
    """Returns {"assets": {scene_id: asset}, "missing": [scene_id], "coverage": float}.
    only=[scene_ids] re-sources just those scenes (targeted retry); existing = previous assets."""
    search, rank, download = search or default_search, rank or default_rank, download or default_download
    os.makedirs(folder, exist_ok=True)
    assets = dict(existing or {})
    banned = set(load_used_ids(meta_get))
    banned |= {a["pexels_id"] for sid, a in assets.items()
               if a.get("status") == "ok" and a.get("pexels_id") and (only is None or sid not in only)}
    for sc in scenes:
        if only is not None and sc.scene_id not in only:
            continue
        got = _source_scene(sc, bible, cfg, banned, folder, search, rank, download, broaden=broaden)
        if got:
            assets[sc.scene_id] = got
        else:
            assets[sc.scene_id] = {"scene_id": sc.scene_id, "status": "missing", "path": "", "pexels_id": None,
                                   "query": " | ".join(_queries_for(sc, bible, broaden)), "relevance": 0.0,
                                   "source": "none", "note": "no relevant stock clip found"}
    _apply_same_location_reuse(scenes, assets)
    return summarize(scenes, assets)


def _apply_same_location_reuse(scenes, assets):
    """A scene with no clip may reuse the clip of an earlier scene in the SAME location
    (that is continuity, not filler). It is flagged so the reviewer/report can show it."""
    by_loc = {}
    for sc in scenes:
        a = assets.get(sc.scene_id, {})
        if a.get("status") == "ok":
            by_loc.setdefault(sc.location, a)
        elif a.get("status") in ("missing", "reused_same_location") and sc.location in by_loc:
            src = by_loc[sc.location]
            assets[sc.scene_id] = {**a, "status": "reused_same_location", "path": src["path"],
                                   "pexels_id": src["pexels_id"], "relevance": src["relevance"],
                                   "note": f"reuses the clip of an earlier scene in the same location ({src['scene_id']})"}


def summarize(scenes, assets):
    usable = [s.scene_id for s in scenes if assets.get(s.scene_id, {}).get("status") in ("ok", "reused_same_location")]
    missing = [s.scene_id for s in scenes if s.scene_id not in usable]
    return {"assets": assets, "missing": missing, "coverage": round(len(usable) / max(len(scenes), 1), 3)}


def retry_missing(scenes, bible, cfg, meta_get, result, **kw):
    """Targeted retries: only scenes still missing, broader query + any orientation.
    Never re-fetches scenes that already have a good clip."""
    for attempt in range(1, cfg.max_scene_retries + 1):
        if not result["missing"]:
            break
        print(f"[horror clips] retry {attempt}/{cfg.max_scene_retries} for scenes {result['missing']}")
        result = source_clips(scenes, bible, cfg, meta_get, only=list(result["missing"]),
                              existing=result["assets"], broaden=True, **kw)
    return result


def veo_fill(scenes, bible, cfg, result, generate=None, folder=CLIP_FOLDER, max_clips=2):
    """OPTIONAL (HORROR_VEO=1, default off): fill scenes still missing using the repo's existing
    Veo helper (agents/veo_agent.py, GEMINI_API_KEY). Uses clip index 90+ so it never overwrites the
    English pipeline's flow clips. Text descriptions only: identity consistency is NOT guaranteed."""
    if os.environ.get("HORROR_VEO", "0") != "1" or not result["missing"]:
        return result
    if generate is None:
        from agents import veo_agent
        generate = lambda prompt, i: veo_agent._generate_single_clip(prompt, 90 + i, "9:16", 8)
    made = 0
    for sc in scenes:
        if sc.scene_id not in result["missing"] or made >= max_clips:
            continue
        loc, chars = bible.get(sc.location), [bible.get(c) for c in sc.characters if bible.get(c)]
        prompt = (f"Cinematic horror, vertical 9:16, 8 seconds, photorealistic, no text. {sc.action}. "
                  f"Location: {loc.description if loc else ''}. Lighting: {sc.lighting or (loc.lighting if loc else '')}. "
                  + " ".join(f"{c.name}: {c.appearance}, wearing {c.clothing}." for c in chars))
        try:
            tmp = generate(prompt, made)
        except Exception as e:
            print(f"[horror veo] {sc.scene_id} failed: {e}")
            continue
        made += 1
        if tmp and os.path.exists(tmp):
            dest = os.path.join(folder, f"{sc.scene_id}.mp4")
            os.replace(tmp, dest)
            result["assets"][sc.scene_id] = {"scene_id": sc.scene_id, "status": "ok", "path": dest, "pexels_id": None,
                                             "query": "veo", "relevance": 1.0, "source": "veo", "note": "AI-generated (Veo)"}
    return summarize(scenes, result["assets"])
