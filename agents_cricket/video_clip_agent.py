# agents_cricket/video_clip_agent.py
"""Cricket-specific Pexels *video* clip fetcher — the moving-B-roll twin of
agents_cricket/image_agent.py's static Pexels chain. Mirrors the same
structured priority order (venue -> team -> generic), minus the player-photo
step (there's no reliable source of real per-player video clips the way
CricAPI's match_squad gives us real photos).

Falls back gracefully per-slot: any failed venue/team/generic search is just
skipped. The caller (scheduler_cricket.py) falls back entirely to
generate_backgrounds() (static images) if too few clips come back overall,
same pattern scheduler_hindi.py / scheduler.py already use.
"""
import os
import random
import requests

from agents.video_clip_agent import search_pexels_video
from agents_hindi.video_clip_agent import search_candidates, rank_candidates, _download as _download_candidate

# V2 Phase 1: persistent asset registry (exact/near duplicate, cooldown, overlap).
# Fully optional — if it can't load, the old behaviour runs unchanged.
try:
    from agents_cricket import asset_registry
except Exception as _e:  # pragma: no cover
    asset_registry = None
    print(f"Asset registry unavailable, using legacy clip picking: {_e}")

V2_UNIQUE_CLIPS = os.getenv("CRICKET_V2_UNIQUE_CLIPS", "1") != "0"
SOURCE = "pexels"

# Selected assets of the most recent run: [{"asset_id", "start", "end"}].
# The scheduler passes these to asset_registry.record_usage() after upload.
LAST_SELECTED = []

GENERIC_QUERIES = [
    "cricket stadium crowd",
    "cricket bat ball closeup",
    "cricket player celebration",
    "cricket stadium floodlights",
    "cricket match action",
    "cricket ground aerial",
    "cricket fans stadium",
    "cricket boundary rope",
    "cricket pitch wicket",
    "cricket team huddle",
]


def _download(url, output_path):
    r = requests.get(url, timeout=90, stream=True)
    r.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)


def _try_query(query, output_path, used_urls):
    """Searches Pexels video for `query`, skipping a result already used
    this run (e.g. same team name appearing twice), and downloads it."""
    try:
        url = search_pexels_video(query)
    except Exception as e:
        print(f"Pexels video search failed for '{query}': {e}")
        return False
    if not url or url in used_urls:
        return False
    try:
        _download(url, output_path)
        used_urls.add(url)
        return True
    except Exception as e:
        print(f"Cricket clip download failed for '{query}': {e}")
        return False


def generate_background_clips_cricket(structured=None, num_clips=4):
    """structured: dict from research_agent.get_summary_for_topic — expects
    venue, teams (same shape agents_cricket/image_agent.py already consumes).
    Falls back to fully generic queries if structured is None/empty."""
    folder = "assets/pexels_clips"
    os.makedirs(folder, exist_ok=True)
    for f in os.listdir(folder):
        os.remove(os.path.join(folder, f))

    structured = structured or {}
    venue = structured.get("venue") or ""
    teams = structured.get("teams") or []

    clip_paths, errors = [], []
    used_urls = set()
    slot = 1

    # 1) Venue-specific stock footage
    if venue and slot <= num_clips:
        output_path = os.path.join(folder, f"{slot}.mp4")
        if _try_query(f"{venue} cricket stadium", output_path, used_urls):
            clip_paths.append(output_path)
            slot += 1
        else:
            errors.append(f"No Pexels video for venue '{venue}'")

    # 2) Team-specific stock footage
    for team in teams:
        if slot > num_clips:
            break
        output_path = os.path.join(folder, f"{slot}.mp4")
        if _try_query(f"{team} cricket team", output_path, used_urls):
            clip_paths.append(output_path)
            slot += 1
        else:
            errors.append(f"No Pexels video for team '{team}'")

    # 3) Generic fallback for any remaining slots — shuffled each run so a
    # news item (venue=None, teams=[]) doesn't always draw the same clips.
    generic_pool = GENERIC_QUERIES[:]
    random.shuffle(generic_pool)
    gi = 0
    while slot <= num_clips and gi < len(generic_pool):
        output_path = os.path.join(folder, f"{slot}.mp4")
        if _try_query(generic_pool[gi], output_path, used_urls):
            clip_paths.append(output_path)
            slot += 1
        gi += 1

    return clip_paths, errors


# ─────────────────────────────────────────────
# V2 Phase 1 — unique clip picking
# ─────────────────────────────────────────────

def _slot_queries(structured, num_clips):
    """One query list per slot: venue -> teams -> shuffled generic (same
    priority order as the legacy fetcher), each with a broader fallback."""
    structured = structured or {}
    venue = structured.get("venue") or ""
    teams = structured.get("teams") or []
    slots = []
    if venue:
        slots.append([f"{venue} cricket stadium", "cricket stadium"])
    for team in teams:
        slots.append([f"{team} cricket team", "cricket team"])
    generic = GENERIC_QUERIES[:]
    random.shuffle(generic)
    for g in generic:
        slots.append([g])
    return slots


def _pick_unique_clip(queries, output_path, banned_ids, target_sec, structured):
    """Searches, filters cheap-first, downloads the best candidates one at a
    time and returns the first that passes hash checks. None if nothing fits."""
    pools = [search_candidates(q) for q in queries]
    ranked = rank_candidates(pools, banned_ids, target_sec, channel="cricket")

    tried = 0
    for cand in ranked:
        if tried >= 4:  # cap downloads per slot
            break
        end = float(min(cand["duration"] or target_sec, max(target_sec, 3)))
        ok, why = asset_registry.check_source_segment(SOURCE, cand["id"], 0.0, end)
        if not ok:
            print(f"    skip pexels id={cand['id']}: {why}")
            banned_ids.add(cand["id"])
            continue
        tried += 1
        try:
            _download_candidate(cand, output_path)
        except Exception as e:
            print(f"    download failed id={cand['id']}: {e}")
            continue
        chk = asset_registry.check_file(output_path)
        if not chk["ok"]:
            print(f"    reject pexels id={cand['id']}: {chk['reason']}")
            banned_ids.add(cand["id"])
            continue
        teams = (structured or {}).get("teams") or []
        asset_id = asset_registry.register_asset(
            SOURCE, cand["id"], cand.get("url", ""), output_path, 0.0, end,
            file_hash=chk["file_hash"], perceptual_hash=chk["perceptual_hash"],
            duration=cand["duration"], width=cand["width"], height=cand["height"],
            team=", ".join(teams) or None,
        )
        banned_ids.add(cand["id"])
        return {"asset_id": asset_id, "start": 0.0, "end": end, "pexels_id": cand["id"]}
    return None


def generate_background_clips_cricket_v2(structured=None, num_clips=4, script_words=90):
    """Same contract as generate_background_clips_cricket(): (clip_paths, errors).
    Never reuses a clip that is on cooldown, overlaps a recent segment, or is an
    exact/near duplicate of one used recently; also never repeats within a video."""
    global LAST_SELECTED
    LAST_SELECTED = []

    folder = "assets/pexels_clips"
    os.makedirs(folder, exist_ok=True)
    for f in os.listdir(folder):
        os.remove(os.path.join(folder, f))

    target_sec = max(script_words / 2.7, 10) / num_clips
    banned, clip_paths, errors = set(), [], []
    slot = 1
    for queries in _slot_queries(structured, num_clips):
        if slot > num_clips:
            break
        out = os.path.join(folder, f"{slot}.mp4")
        picked = _pick_unique_clip(queries, out, banned, target_sec, structured)
        if picked:
            clip_paths.append(out)
            LAST_SELECTED.append(picked)
            print(f"  Clip {slot}: pexels id={picked['pexels_id']} asset={picked['asset_id']}")
            slot += 1
        else:
            errors.append(f"No unique clip for {queries}")
    return clip_paths, errors


def pick_clips(structured=None, num_clips=4, script_words=90):
    """Entry point for the scheduler: V2 unique picker when enabled and the
    registry is available, otherwise the legacy fetcher."""
    if V2_UNIQUE_CLIPS and asset_registry is not None and asset_registry.ensure_tables():
        try:
            return generate_background_clips_cricket_v2(structured, num_clips, script_words)
        except Exception as e:
            print(f"V2 clip picker failed ({e}) — falling back to legacy picker")
    return generate_background_clips_cricket(structured, num_clips=num_clips)
