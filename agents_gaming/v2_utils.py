# agents_gaming/v2_utils.py
"""Small shared helpers for the Gaming V2 agents (hook / commentary / quality).

Mirrors agents_cricket/story_utils.py (number-grounding guard, script
cleaning) but kept separate: gaming scripts are English, cricket's are Hindi.
"""
import json
import re

# Phrases from the Gaming V2 spec (section 7) that make a channel read as
# automated. Not banned outright — repeated use must trigger regeneration.
AI_PHRASES = [
    "you won't believe",
    "you wont believe",
    "nobody expected",
    "this insane moment",
    "absolutely incredible",
    "let's take a look",
    "lets take a look",
    "watch what happens",
    "crazy gaming moment",
]

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def clean_script(text):
    """Strip labels/markdown/quotes the LLM sometimes wraps around a script."""
    text = re.sub(r"[*#_`>]+", "", text or "")
    text = re.sub(r"^\s*(hook|setup|reaction|payoff|script|commentary)\s*[:\-]\s*",
                  "", text, flags=re.I | re.M)
    text = text.strip().strip('"\u201c\u201d')
    # keep line breaks (they become pauses) but squeeze runs of spaces
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def word_count(text):
    return len((text or "").split())


def extract_json(text):
    """Pull the first JSON object out of an LLM reply (handles ```json fences
    and chatter around it). Returns a dict, or None if nothing parses."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def numbers_in(text):
    return set(_NUM_RE.findall(text or ""))


def ungrounded_numbers(script, source_text):
    """Digit tokens the script states that appear nowhere in the source data
    (clip title / moment analysis). Catches invented stats like '5 kills'."""
    return sorted(numbers_in(script) - numbers_in(source_text), key=float)


def find_ai_phrases(text):
    low = (text or "").lower().replace("\u2019", "'")
    return [p for p in AI_PHRASES if p in low]


def repeated_ai_phrases(text, recent_texts=None):
    """AI-pattern phrases that should trigger regeneration: any phrase also
    used in a recent script, or two+ distinct AI phrases in the same script."""
    hits = find_ai_phrases(text)
    if not hits:
        return []
    recent_low = " ".join(recent_texts or []).lower().replace("\u2019", "'")
    repeated = [p for p in hits if p in recent_low]
    if len(hits) >= 2:
        return sorted(set(hits))
    return repeated


# Claims the writer likes to invent. Only allowed when the clip data itself says it.
_RISKY_CLAIM_WORDS = ("solo", "alone", "single-handedly", "one-man", "by himself", "by herself")
_STAGE_DIRECTION_RE = re.compile(
    r"\b(smirks?|grins?|smiles?|flashes|pops up|appears on screen|banner|camera (?:cuts|pans|zooms))\b",
    re.I)


def ungrounded_claims(text, source_text):
    """Risky claim words (solo / alone / ...) used in `text` but absent from the clip data."""
    low, src = (text or "").lower(), (source_text or "").lower()
    return [w for w in _RISKY_CLAIM_WORDS
            if re.search(r"\b" + re.escape(w) + r"\b", low) and not re.search(r"\b" + re.escape(w) + r"\b", src)]


def stage_directions(text):
    """Lines that describe what is on screen ('Ace banner flashes, he smirks') instead of being spoken."""
    return sorted({m.group(0).lower() for m in _STAGE_DIRECTION_RE.finditer(text or "")})


def clamp(value, lo, hi, default=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))
