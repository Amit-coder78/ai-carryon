# agents_horror/story_agent.py
"""Stages 1, 3 and 4: story idea, scriptwriter, story bible."""
import re

from agents_horror.config import CATEGORIES, FICTION_LABEL
from agents_horror.llm import parse_json
from agents_horror.schemas import StoryBible, validate_bible

STOCK_OPENERS = ("it was a dark and stormy night", "have you ever", "let me tell you",
                 "this is a story", "gather round", "once upon a time", "this is the story of")
REAL_CLAIMS = ("true story", "real story", "really happened", "actual events", "real incident",
               "based on a true", "caught on camera", "real footage")
_STAGE_DIRECTION = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")
_LABEL = re.compile(r"^\s*(hook|setup|suspense|escalation|reveal|ending|narrator|scene\s*\d+)\s*[:\-–]\s*",
                    re.IGNORECASE | re.MULTILINE)


def split_sentences(text):
    parts = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def clean_script(text):
    t = (text or "").replace("```", "")
    t = _STAGE_DIRECTION.sub("", t)
    t = _LABEL.sub("", t)
    t = re.sub(r"[#*_>`]+", "", t)
    t = re.sub(r"\s*\n+\s*", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


INTENSITY = {
    1: "quiet unease; very restrained, almost nothing is shown",
    2: "slow creeping dread with small, subtle wrongness",
    3: "steady rising tension with one clear frightening moment",
    4: "high tension, relentless pressure, a strong frightening reveal",
    5: "maximum suspense and dread; intense, but never graphic",
}


# ── Stage 1: concept ────────────────────────────────────────────────────

def generate_concept(cfg, category, budget, recent_premises=(), series=None):
    """One LLM call -> {premise, central_mystery, main_character, ending_direction,
    setting, research_topic}. Raises if the model gives nothing usable."""
    avoid = "\n".join(f"- {p[:140]}" for p in list(recent_premises)[-8:]) or "- (none yet)"
    series_block = ""
    if cfg.mode == "episodic" and series and series.get("episode"):
        past = "\n".join(f"- Ep {s['episode']}: {s['summary']}" for s in series["summaries"][-5:])
        series_block = (f"\nThis is EPISODE {series['episode'] + 1} of the series \"{series.get('title') or 'untitled'}\".\n"
                        f"Previous episodes:\n{past}\nContinue the storyline with the SAME recurring characters, "
                        f"locations and entity. Do not retell earlier episodes.\n")
    idea_line = (f"The creator's idea (build on it, do not copy it word for word): {cfg.idea}\n"
                 if cfg.idea else "Invent a fresh original premise.\n")
    setting_line = f"Setting: {cfg.setting}\n" if cfg.setting else "Choose a specific, concrete setting.\n"
    prompt = f"""You are a horror story editor for the YouTube channel "{cfg.channel_name}".
Audience: {cfg.audience}. Language: English.
Category: {category} ({CATEGORIES[category]}).
Format: {"long-form story" if cfg.is_long else "YouTube Short"}, about {cfg.duration_sec} seconds of narration.
Intensity: {cfg.intensity}/5 ({INTENSITY[cfg.intensity]}).
{idea_line}{setting_line}{series_block}
Do NOT repeat these recent premises:
{avoid}

Rules: the story is FICTION. No real people, no real crimes, no fake "true story" claims.
No gore, no sexual content, no violence against children. Folklore may inspire the story, but invent the events.

Return ONLY JSON:
{{"premise": "2 sentences", "central_mystery": "the question the viewer wants answered",
 "main_character": "one line", "ending_direction": "how it ends (twist, payoff or cliffhanger)",
 "setting": "specific place and time", "research_topic": "real place or folklore name worth fact-checking, or empty string"}}"""
    data = parse_json(budget.ask(prompt))
    if not isinstance(data, dict) or not str(data.get("premise", "")).strip():
        raise ValueError("concept: model did not return a premise")
    return {k: str(data.get(k, "") or "").strip() for k in
            ("premise", "central_mystery", "main_character", "ending_direction", "setting", "research_topic")} | {
        "category": category}


# ── Stage 4: story bible ────────────────────────────────────────────────

def build_bible(cfg, concept, budget, series=None):
    """Persistent story bible. Episodes 2+ of a series reuse the stored bible (0 LLM calls)."""
    if cfg.mode == "episodic" and series and series.get("bible"):
        bible = StoryBible.from_dict(series["bible"])
        if not validate_bible(bible):
            return bible
    prompt = f"""Create the STORY BIBLE for this original horror story so every scene stays consistent.

Premise: {concept['premise']}
Central mystery: {concept['central_mystery']}
Main character: {concept['main_character']}
Setting: {concept['setting']}
Ending direction: {concept['ending_direction']}

Return ONLY JSON with this shape (ids are short lowercase snake_case):
{{"title": "...", "palette": "overall colour palette", "time_of_day": "...", "weather": "...",
 "characters": [{{"id": "mira", "name": "...", "age": "21", "appearance": "face, hair, build, distinctive marks",
   "clothing": "e.g. black hoodie, blue jeans", "features": "...", "role": "protagonist",
   "relationships": {{"other_id": "relationship"}}}}],
 "locations": [{{"id": "hostel_corridor", "name": "...", "description": "...", "layout": "...",
   "lighting": "...", "palette": "...", "weather": "...", "time_of_day": "..."}}],
 "props": [{{"id": "...", "name": "...", "description": "..."}}],
 "entities": [{{"id": "...", "name": "...", "description": "fixed look and behaviour", "rules": "what it can and cannot do"}}],
 "continuity_rules": ["short rules, e.g. the corridor light always flickers"]}}
Use 1-3 characters, 1-3 locations, 0-3 props, 0-1 entity. Every character needs appearance AND clothing."""
    last_errs = []
    for _ in range(2):
        data = parse_json(budget.ask(prompt if not last_errs else
                                     prompt + f"\n\nYour previous answer had these problems, fix them: {last_errs}"))
        bible = StoryBible.from_dict(data if isinstance(data, dict) else {})
        last_errs = validate_bible(bible)
        if not last_errs:
            return bible
        if budget.remaining < 3:  # keep calls for script + scenes + seo
            break
    raise ValueError(f"story bible invalid: {last_errs}")


# ── Stage 3: script ─────────────────────────────────────────────────────

def _structure(cfg):
    if cfg.is_long:
        return ("Act 1: hook, character, location, normal world. Act 2: first strange event, warning, growing doubt. "
                "Act 3: escalation across several beats with environmental storytelling. "
                "Act 4: frightening reveal and climax. Act 5: meaningful ending that follows from the setup.")
    return ("1 HOOK (first 1-3 seconds, under 15 words, a concrete unsettling image or line). 2 SETUP (character + place). "
            "3 SUSPENSE (an unusual event or a warning). 4 ESCALATION (tension rises through real developments). "
            "5 REVEAL (a frightening or surprising discovery). 6 ENDING (twist, payoff or natural cliffhanger).")


def generate_script(cfg, concept, bible, budget, episode=None, research=None):
    """One LLM call -> (script, episode_summary)."""
    bible_brief = "; ".join(
        [f"{c.name} ({c.age}, {c.appearance}, wearing {c.clothing})" for c in bible.characters] +
        [f"place: {l.name} - {l.description}" for l in bible.locations] +
        [f"entity: {e.name} - {e.description}" for e in bible.entities])
    facts = ""
    if research and research.get("used"):
        facts = ("\nDocumented background (keep these facts accurate, never attach invented events to real people):\n- "
                 + "\n- ".join(research["documented_facts"]) + "\n")
    ep_line = f"This is episode {episode} of a series; end with a hook for the next episode.\n" if episode and episode > 1 else \
              (f"This is episode 1 of a series; end with a hook for episode 2.\n" if episode == 1 else "")
    prompt = f"""Write the narration script for an original English horror story.

Premise: {concept['premise']}
Central mystery: {concept['central_mystery']}
Ending direction: {concept['ending_direction']}
Story bible (stay consistent with it): {bible_brief}
Continuity rules: {'; '.join(bible.continuity_rules) or 'none'}
{facts}{ep_line}
Structure: {_structure(cfg)}
Length: about {cfg.target_words} words (a hard limit: between {int(cfg.target_words * 0.85)} and {int(cfg.target_words * 1.15)}).
Intensity: {cfg.intensity}/5 - {INTENSITY[cfg.intensity]}.

STRICT RULES:
- Plain spoken narration only. No stage directions, no brackets, no labels like "Hook:", no hashtags, no emoji.
- Short punchy sentences. Dialogue is brief and clear. Pauses can be shown with "..." sparingly.
- Original story. The ending must follow from the setup. No gore. Never say or imply it is a true story or a real incident.
- First sentence is the hook, under 15 words. Do not open with a cliche such as "It was a dark and stormy night".

Return ONLY JSON: {{"script": "...", "episode_summary": "one sentence summary of what happens"}}"""
    raw = budget.ask(prompt)
    data = parse_json(raw)
    if isinstance(data, dict) and data.get("script"):
        script, summary = clean_script(str(data["script"])), str(data.get("episode_summary", "")).strip()
    else:  # model ignored the JSON instruction: accept plain text
        script, summary = clean_script(raw), ""
    if not script:
        raise ValueError("script: empty after cleaning")
    return script, summary or concept["premise"]


def rewrite_to_length(cfg, script, budget, target_words=None):
    """One targeted rewrite when the script is far from the target length."""
    n = len(script.split())
    target = target_words or cfg.target_words
    direction = "Shorten" if n > target else "Expand"
    prompt = (f"{direction} this horror narration to about {target} words ({n} now). Keep the hook, the story "
              f"beats, the reveal and the ending. Plain narration only, no labels or stage directions.\n"
              f"Return ONLY the script text.\n\n{script}")
    return clean_script(budget.ask(prompt))


def validate_script(script, cfg):
    """Returns (errors, warnings). Errors block rendering, warnings go in the review report."""
    errs, warns = [], []
    words = script.split()
    lo, hi = int(cfg.target_words * 0.75), int(cfg.target_words * 1.25)
    if len(words) < lo:
        errs.append(f"script too short: {len(words)} words (target {cfg.target_words})")
    elif len(words) > hi:
        errs.append(f"script too long: {len(words)} words (target {cfg.target_words})")
    sentences = split_sentences(script)
    if len(sentences) < (4 if not cfg.is_long else 12):
        errs.append("script has too few sentences")
    low = script.lower()
    for phrase in REAL_CLAIMS:
        if phrase in low:
            errs.append(f"script presents fiction as real ('{phrase}')")
    if sentences:
        if len(sentences[0].split()) > 18:
            warns.append("hook is long (over 18 words)")
        if sentences[0].lower().startswith(STOCK_OPENERS):
            warns.append("hook is a stock opener")
    if re.search(r"\[[^\]]*\]|\([^)]*\)", script):
        errs.append("script contains stage directions")
    return errs, warns


def fiction_description_line():
    return FICTION_LABEL
