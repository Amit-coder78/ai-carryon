# agents/video_clip_agent.py
"""
Fetches topic-relevant vertical video clips from Pexels' Videos API
(free, same API key as Pexels Photos — https://www.pexels.com/api/)
to use as dynamic B-roll instead of static Ken-Burns images.

Falls back gracefully: if a search term returns nothing, or the
Pexels video endpoint errors out, that slot is just skipped — the
caller (scheduler.py) falls back to generate_backgrounds() (static
images) if this returns too few clips to make a decent video.
"""
import os
import re
import requests

from agents.image_agent import generate_image_prompts  # reuse existing LLM prompt generation


def _pick_best_video_file(video_files):
    """
    Pexels returns several encodes per clip (different widths/qualities).
    Prefer something close to 720-1080px wide — good visual quality for
    Shorts without downloading an oversized 4K file every time.
    """
    if not video_files:
        return None
    candidates = [f for f in video_files if f.get("file_type") == "video/mp4"]
    if not candidates:
        candidates = video_files
    candidates.sort(key=lambda f: abs((f.get("width") or 0) - 1080))
    return candidates[0]["link"]


def _ranker_on(channel):
    """Semantic clip ranking on? Never raises; any problem means 'off' (old behaviour)."""
    try:
        from agents import clip_ranker
        return clip_ranker.enabled(channel)
    except Exception:
        return False


def search_pexels_video(query, min_duration=3, max_duration=25, channel="english", used_ids=None):
    pexels_key = os.getenv("PEXELS_API_KEY", "")
    if not pexels_key:
        return None
    use_rank = _ranker_on(channel)
    headers = {"Authorization": pexels_key}
    params = {
        "query": query,
        "orientation": "portrait",
        "size": "medium",
        # semantic ranking needs a pool; still ONE Pexels request per clip
        "per_page": 15 if use_rank else 1,
        "min_duration": min_duration,
        "max_duration": max_duration,
    }
    r = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    videos = data.get("videos", [])
    if not videos:
        return None
    if not use_rank:
        return _pick_best_video_file(videos[0].get("video_files", []))
    try:
        from agents import clip_ranker
        cands = [{"id": v.get("id"), "url": v.get("url", ""), "width": v.get("width") or 0,
                  "height": v.get("height") or 0, "duration": v.get("duration") or 0,
                  "files": v.get("video_files", [])} for v in videos if v.get("id")]
        ranked = clip_ranker.rank_for_query(
            query, cands, min_duration, max_duration, exclude_ids=used_ids,
            extra_reject=clip_ranker.CHANNEL_REJECT.get(channel, ()))
    except Exception as e:  # ranker bug must never block a render: use the old pick
        print(f"  clip_ranker failed ({e}) - using first Pexels result")
        return _pick_best_video_file(videos[0].get("video_files", []))
    if not ranked:
        return None  # nothing relevant: caller retries with a broader query
    if used_ids is not None:
        used_ids.add(ranked[0]["id"])
    return _pick_best_video_file(ranked[0]["files"])


def download_video_clip(prompt, output_path, channel="english", used_ids=None):
    clean_prompt = re.sub(r"[^a-zA-Z0-9 ,]", "", prompt).strip()
    search_query = " ".join(clean_prompt.split()[:5])
    # Retry once with a shorter, broader query (e.g. just the first 2 words)
    broad_query = " ".join(clean_prompt.split()[:2])
    if _ranker_on(channel):
        from agents import clip_ranker
        # filler words dropped BEFORE trimming so the noun survives; the retry
        # keeps the two longest subject words instead of possibly just "close up"
        search_query = clip_ranker.compact_query(clean_prompt, 5) or search_query
        broad_query = clip_ranker.broaden_query(clean_prompt) or broad_query

    video_url = search_pexels_video(search_query, channel=channel, used_ids=used_ids)
    if not video_url:
        if broad_query != search_query:
            video_url = search_pexels_video(broad_query, channel=channel, used_ids=used_ids)

    if not video_url:
        raise RuntimeError(f"No Pexels video found for query: '{search_query}'")

    resp = requests.get(video_url, timeout=90, stream=True)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    return output_path


def generate_background_clips(topic, script, num_clips=4):
    folder = "assets/pexels_clips"
    os.makedirs(folder, exist_ok=True)
    for f in os.listdir(folder):
        os.remove(os.path.join(folder, f))

    clean_topic = topic.split("||PATTERN:")[0].strip()
    prompts = generate_image_prompts(clean_topic, script, num_clips)

    print(f"\nSearching Pexels video clips for: {clean_topic}")
    for i, p in enumerate(prompts):
        print(f"  {i+1}: {p}")

    clip_paths = []
    errors = []
    used_ids = set()  # no clip repeated inside one video
    for i, prompt in enumerate(prompts):
        output_path = os.path.join(folder, f"{i+1}.mp4")
        try:
            download_video_clip(prompt, output_path, channel="english", used_ids=used_ids)
            clip_paths.append(output_path)
            print(f"Clip {i+1}: downloaded")
        except Exception as e:
            errors.append(f"Clip {i+1}: {e}")
            print(f"Clip {i+1} failed: {e}")

    return clip_paths, errors


# ─────────────────────────────────────────────
# Hook search (Hook Engine) — same Pexels client, second use case.
#
#   Pexels API
#     ├── normal clips : search_pexels_video() / download_video_clip()  (above)
#     └── hook clips   : search_hook_videos()  / download_video_file()  (below)
#
# No second API client: both paths use PEXELS_API_KEY and the same
# https://api.pexels.com/videos/search endpoint. See agents/hook_engine.py.
# ─────────────────────────────────────────────
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"


def search_videos(query, per_page=10, orientation="portrait", size=None):
    """Search Pexels videos and return NORMALIZED candidates (never raises):
    [{"id", "width", "height", "duration", "url", "files", "query"}].
    Same shape as agents_hindi.video_clip_agent.search_candidates()."""
    key = os.getenv("PEXELS_API_KEY", "")
    if not key:
        return []
    params = {"query": query, "per_page": per_page}
    if orientation:
        params["orientation"] = orientation
    if size:
        params["size"] = size
    try:
        r = requests.get(PEXELS_VIDEO_SEARCH_URL, headers={"Authorization": key},
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


def search_hook_videos(queries, orientation="portrait", duration_min=1, duration_max=10,
                       per_query=6, max_total=20, exclude_ids=None):
    """Hook-specific search over several queries (spec: 3-5 queries, ~10-20
    candidates). Duplicates across queries are dropped (first query wins) and
    the duration window is applied client-side, because the Pexels videos
    endpoint has no duration filter. Returns a list of normalized candidates."""
    exclude = {str(x) for x in (exclude_ids or [])}
    seen, out = set(), []
    for q in queries or []:
        for c in search_videos(q, per_page=per_query, orientation=orientation):
            cid = str(c["id"])
            if cid in seen or cid in exclude:
                continue
            d = c.get("duration") or 0
            if duration_min is not None and d < duration_min:
                continue
            if duration_max is not None and d > duration_max:
                continue
            seen.add(cid)
            out.append(c)
            if len(out) >= max_total:
                return out
    return out


def download_video_file(candidate, output_path):
    """Download a normalized candidate's best mp4 encode. Raises on failure."""
    link = _pick_best_video_file(candidate.get("files") or [])
    if not link:
        raise RuntimeError("no downloadable file for candidate")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    resp = requests.get(link, timeout=90, stream=True)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    return output_path
