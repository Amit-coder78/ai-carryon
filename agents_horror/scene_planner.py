# agents_horror/scene_planner.py
"""Stage 5: turn the approved script into an ordered scene plan.

The LLM only decides how to GROUP sentences and what each scene looks/sounds like.
Narration text is always rebuilt from the script itself, so scenes can never drop,
invent or reorder narration."""
import re

from agents_horror.config import WORDS_PER_SECOND
from agents_horror.llm import parse_json
from agents_horror.schemas import Scene, validate_scenes
from agents_horror.story_agent import split_sentences

_STOP = {"the", "a", "an", "and", "of", "in", "on", "at", "to", "with", "his", "her", "their", "its"}


def target_scene_count(cfg):
    per = 8 if cfg.is_long else 6.5
    return max(4, min(60 if cfg.is_long else 10, round(cfg.duration_sec / per)))


def estimate_duration(text):
    return max(1.5, len(text.split()) / WORDS_PER_SECOND)


def clean_keyword(q):
    q = re.sub(r"[^A-Za-z0-9 ]+", " ", str(q or "")).strip().lower()
    words = [w for w in q.split() if w not in _STOP or len(q.split()) <= 2]
    return " ".join(words[:4])


def _assign_times(scenes, pause_default=0.3):
    t = 0.0
    for sc in scenes:
        sc.duration = round(estimate_duration(sc.narration) + sc.pause_after, 2)
        sc.start = round(t, 2)
        t += sc.duration
    return scenes


def retime_from_actual(scenes, actual_seconds):
    """After TTS: replace the estimates with the real per-scene audio lengths."""
    t = 0.0
    for sc, dur in zip(scenes, actual_seconds):
        sc.duration, sc.start = round(dur, 3), round(t, 3)
        t += dur
    return scenes


def _fallback_group(sentences, n):
    """Even grouping by word count when the LLM plan is unusable."""
    total = sum(len(s.split()) for s in sentences) or 1
    goal, groups, cur, cur_words = total / n, [], [], 0
    for i, s in enumerate(sentences):
        cur.append(i)
        cur_words += len(s.split())
        if cur_words >= goal and len(groups) < n - 1:
            groups.append((cur[0], cur[-1]))
            cur, cur_words = [], 0
    if cur:
        groups.append((cur[0], cur[-1]))
    return groups


def _scene_from_plan(i, item, sentences, bible, first_loc):
    a = max(0, int(item.get("first_sentence", 1)) - 1)
    b = min(len(sentences) - 1, int(item.get("last_sentence", a + 1)) - 1)
    valid = bible.all_ids()
    loc = item.get("location") if item.get("location") in valid else first_loc
    chars = [c for c in (item.get("characters") or []) if c in valid]
    refs = [r for r in (item.get("bible_refs") or []) if r in valid]
    for r in [loc, *chars]:
        if r and r not in refs:
            refs.append(r)
    kws = [clean_keyword(k) for k in (item.get("keywords") or item.get("search_keywords") or [])]
    kws = [k for k in kws if k][:3]
    return Scene(
        scene_id=f"s{i}", order=i, narration=" ".join(sentences[a:b + 1]),
        characters=chars, location=loc, action=str(item.get("action", "")).strip(),
        camera=str(item.get("camera", "")).strip(), lighting=str(item.get("lighting", "")).strip(),
        atmosphere=str(item.get("atmosphere", "")).strip(), bible_refs=refs, search_keywords=kws,
        sfx=[str(x).strip() for x in (item.get("sfx") or []) if str(x).strip()][:2],
        ambience=str(item.get("ambience", "")).strip(),
        transition=str(item.get("transition", "cut")).strip() or "cut",
        continuity=[str(x) for x in (item.get("continuity") or [])][:3],
        pause_after=min(1.2, max(0.15, float(item.get("pause_after", 0.3) or 0.3))),
    ), (a, b)


def plan_scenes(cfg, script, bible, budget):
    sentences = split_sentences(script)
    n = min(target_scene_count(cfg), len(sentences))
    first_loc = bible.locations[0].id
    ids = ", ".join(sorted(bible.all_ids()))
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1))
    prompt = f"""You are a horror video editor planning {n} scenes for a vertical video.
Group the numbered narration sentences into exactly {n} consecutive scenes (every sentence in exactly one scene, in order).

Valid story-bible ids: {ids}
Narration:
{numbered}

For each scene return: first_sentence, last_sentence, location (a bible id), characters (bible ids, may be empty),
action (what the camera sees), camera (angle + movement), lighting, atmosphere,
keywords (3 stock-footage search queries, 2-4 words each, most specific first: environments, objects, silhouettes, weather.
No people's names, no brands, no abstract ideas), sfx (0-2 short sound effects such as "door creak"),
ambience (one short phrase such as "rain on roof"), transition ("cut" or "fade"), pause_after (seconds, 0.2-1.0; longer before the reveal).
Keep the same location lighting for scenes in the same place. Do not add jump scares to every scene.

Return ONLY JSON: [{{"first_sentence":1,"last_sentence":2,"location":"...","characters":[],"action":"...","camera":"...",
"lighting":"...","atmosphere":"...","keywords":["...","...","..."],"sfx":[],"ambience":"...","transition":"cut","pause_after":0.3}}]"""
    scenes = None
    try:
        plan = parse_json(budget.ask(prompt))
        if isinstance(plan, dict):
            plan = plan.get("scenes")
        if isinstance(plan, list) and plan:
            built, covered, expected = [], [], 0
            for i, item in enumerate(plan, 1):
                if not isinstance(item, dict):
                    raise ValueError("bad scene item")
                sc, span = _scene_from_plan(i, item, sentences, bible, first_loc)
                if span[0] != expected:  # gap/overlap -> not trustworthy
                    raise ValueError(f"sentence coverage broken at scene {i}")
                expected = span[1] + 1
                built.append(sc)
            if expected != len(sentences):
                raise ValueError("plan does not cover every sentence")
            scenes = built
    except Exception as e:
        print(f"[horror scene planner] LLM plan unusable ({e}) - using deterministic grouping")
    if scenes is None:
        scenes = []
        for i, (a, b) in enumerate(_fallback_group(sentences, n), 1):
            sc, _ = _scene_from_plan(i, {"first_sentence": a + 1, "last_sentence": b + 1}, sentences, bible, first_loc)
            scenes.append(sc)
    for sc in scenes:
        if not sc.search_keywords:
            sc.search_keywords = default_keywords(sc, bible)
    _assign_times(scenes)
    return scenes


def default_keywords(scene, bible):
    """Fallback queries built from THIS scene's bible entries (not a fixed list of clips)."""
    loc = bible.get(scene.location)
    base = clean_keyword(loc.name) if loc else ""
    mood = clean_keyword(scene.atmosphere) or "night"
    out = []
    if base:
        out.append(clean_keyword(f"{base} {mood}"))
        out.append(base)
    if loc and loc.time_of_day:
        out.append(clean_keyword(f"{loc.time_of_day} {base}"))
    return [q for q in dict.fromkeys(out) if q][:3]


def check_plan(cfg, scenes, bible):
    return validate_scenes(scenes, bible, total_sec=cfg.duration_sec)
