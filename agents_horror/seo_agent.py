# agents_horror/seo_agent.py
"""Section 6: metadata. One LLM call (deterministic fallback if the budget/LLM is gone).
Fiction is ALWAYS labelled; titles/descriptions never claim a true story."""
import re

from agents_horror.config import FICTION_LABEL
from agents_horror.llm import parse_json
from agents_horror.story_agent import REAL_CLAIMS

BASE_TAGS = ["horror", "horror story", "scary story", "short horror", "fiction", "creepypasta style",
             "suspense", "supernatural", "ai carryon horror"]
BASE_HASHTAGS = ["#Horror", "#ScaryStory", "#Fiction", "#Shorts"]


def _strip_claims(text):
    out = text or ""
    for phrase in REAL_CLAIMS:
        out = re.sub(re.escape(phrase), "fictional tale", out, flags=re.IGNORECASE)
    return out


def _trim_title(title, limit):
    title = re.sub(r"\s+", " ", title).strip().strip('"')
    if len(title) <= limit:
        return title
    return title[:limit].rsplit(" ", 1)[0].rstrip(",:;-|") or title[:limit]


def _fallback(concept, bible):
    base = bible.title or " ".join(concept["premise"].split()[:6]).rstrip(".,")
    return {"title": base, "description": concept["premise"], "tags": [],
            "thumbnail_text": " ".join(base.upper().split()[:3]),
            "thumbnail_concept": f"Dark close-up of {bible.locations[0].name if bible.locations else 'an empty room'} "
                                 f"with a faint unexplained figure; high contrast, one glowing light."}


def generate_metadata(cfg, concept, bible, script, budget, episode=None, series_title="", research=None,
                      attributions=()):
    limit = 60 if not cfg.is_long else 90
    meta = None
    try:
        prompt = f"""Write YouTube metadata for an ORIGINAL FICTION horror {"Short" if not cfg.is_long else "video"}.
Premise: {concept['premise']}
Script: {script[:900]}
Rules: title under {limit} characters, specific and curious, no clickbait lies, never claim it is a true story or real footage.
Return ONLY JSON: {{"title": "...", "description": "2 sentences, no hashtags", "tags": ["8-12 search terms"],
"thumbnail_text": "max 4 words", "thumbnail_concept": "one sentence visual idea"}}"""
        data = parse_json(budget.ask(prompt))
        if isinstance(data, dict) and str(data.get("title", "")).strip():
            meta = data
    except Exception as e:
        print(f"[horror seo] LLM metadata unavailable ({e}) - using fallback")
    if meta is None:
        meta = _fallback(concept, bible)

    title = _trim_title(_strip_claims(str(meta.get("title", ""))), limit)
    if episode and cfg.mode == "episodic" and "ep" not in title.lower().split()[-2:]:
        suffix = f" (Ep {episode})"
        title = _trim_title(title, limit - len(suffix)) + suffix
    desc = _strip_claims(str(meta.get("description", "")).strip())
    tags = []
    for t in list(meta.get("tags") or []) + BASE_TAGS:
        t = re.sub(r"[#\"<>]", "", str(t)).strip()
        if t and t.lower() not in [x.lower() for x in tags] and len(t) <= 30:
            tags.append(t)
    tags = tags[:15]

    lines = [f"Fiction: {FICTION_LABEL}", "", desc]
    if cfg.mode == "episodic" and episode:
        lines += ["", f"{series_title or 'Series'} - Episode {episode}"]
    if research and research.get("used"):
        lines += ["", "Background sources: " + "; ".join(f"{s['title']} ({s['url']})" for s in research["sources"]),
                  "Everything that happens in the story itself is invented."]
    if attributions:
        from agents_horror.sound_agent import attribution_lines
        lines += ["", *attribution_lines(attributions)]
    description = "\n".join(lines).strip()
    hashtags = list(dict.fromkeys(BASE_HASHTAGS + ["#" + re.sub(r"\W", "", t.title()) for t in tags[:4]]))[:10]
    return {"title": title, "description": description, "tags": tags, "hashtags": hashtags,
            "thumbnail_text": str(meta.get("thumbnail_text", "")).upper()[:40],
            "thumbnail_concept": str(meta.get("thumbnail_concept", "")).strip(),
            "episode": episode if cfg.mode == "episodic" else None,
            "sources": (research or {}).get("sources", [])}
