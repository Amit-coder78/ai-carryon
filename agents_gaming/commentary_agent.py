# agents_gaming/commentary_agent.py
"""
Gaming V2 Phase 4 — original commentary that sounds like a gaming creator,
not an announcer. Style is picked from the moment type. The script follows
the Phase-10 timeline (hook -> setup -> reaction -> payoff -> optional CTA)
and leaves breathing room (`...` and line breaks become pauses in TTS).

Local guards run on every draft and trigger a regeneration:
  - AI-cliche phrases that are repeated (in-script or vs recent scripts)
  - numbers/stats that aren't in the clip data (number-grounding guard, as in cricket)
  - length far outside the clip's duration (the renderer sizes the video to
    the voiceover, so a too-long script loops the clip and a too-short one
    cuts the payoff)
"""
from agents_gaming.v2_utils import (
    clean_script, repeated_ai_phrases, stage_directions, ungrounded_claims,
    ungrounded_numbers, word_count,
)

import os

# The shared renderer currently strips the clip's own audio, so a "..." pause
# is silent dead air today. Pauses become useful in Sprint 2 (game audio kept
# under the voiceover) — flip GAMING_COMMENTARY_PAUSES=1 then.
PAUSES_ENABLED = os.getenv("GAMING_COMMENTARY_PAUSES", "0") == "1"

WORDS_PER_SECOND = 2.4  # matches the pace the legacy 55-90 word / 20-35 s target implied

STYLE_BY_MOMENT = {
    "CLUTCH": "storytelling",
    "FAIL": "funny",
    "FUNNY": "funny",
    "RAGE": "reaction",
    "INSANE_PLAY": "analysis",
    "LUCK": "reaction",
    "UNEXPECTED": "hype",
    "TROLL": "funny",
    "RECORD": "hype",
    "DRAMA": "storytelling",
    "REACTION": "reaction",
}

STYLE_GUIDE = {
    "hype": "Hype. Talk like a friend losing it next to you: short bursts, build to the payoff, "
            "let one line land on its own.",
    "storytelling": "Storytelling. Plain, calm, escalating sentences — set the odds against the player, "
                    "then let the turn-around hit.",
    "funny": "Funny. Dry and deadpan, like roasting a friend's decision. Understate it; the clip "
             "supplies the punchline.",
    "analysis": "Analysis. Point at the specific decision or movement and say why it works, "
                "like a coach on a replay. Concrete, not gushing.",
    "reaction": "Reaction. First-person disbelief. Fragments are fine. Let the pause carry it.",
}


def style_for(moment):
    return STYLE_BY_MOMENT.get((moment or {}).get("moment_type", ""), "hype")


def target_word_range(duration, mode="full"):
    """(min_words, max_words) sized to the clip so the payoff isn't cut or looped.
    "short" = Sprint 2 overlay: a few lines over the clip's own audio (<= ~40% of its length)."""
    d = float(duration or 25)
    if mode == "short":
        hi = max(18, min(30, round(d * 0.9)))
        return 9, hi
    mid = max(35, min(85, round(d * WORDS_PER_SECOND)))
    return int(mid * 0.8), int(mid * 1.15)


def grounding_text(moment, summary, hook=None):
    m = moment or {}
    return " ".join([
        summary or "", (hook or {}).get("text", "") if isinstance(hook, dict) else "",
        str(m.get("game", "")), str(m.get("what_happened", "")), str(m.get("setup", "")),
        str(m.get("payoff", "")), str(m.get("reaction", "")),
    ])


def _build_prompt(moment, hook, summary, structured, style, lo, hi, feedback, mode="full"):
    if mode == "short":
        return _build_short_prompt(moment, hook, summary, style, lo, hi, feedback)
    broadcaster = (structured or {}).get("broadcaster", "")
    hook_line = (f'Open with this exact hook as the first line: "{hook["text"]}"'
                 if hook else "Open with a strong 2-second hook line (no greeting).")
    pause_step = ('3. a beat of silence: write "..." on its own line where the viewer should just watch/listen to the clip\n'
                  if PAUSES_ENABLED else "")
    pause_rule = (' (the "..." pauses don\'t count)' if PAUSES_ENABLED
                  else ' — continuous speech, no "..." and no dead air')
    fb = (f"\nA reviewer rejected the previous draft. Fix this: {feedback}\n" if feedback else "")
    return f"""You are a gaming creator recording the voiceover for a YouTube Short. The viewer
is watching the real gameplay while you talk, so react to it — never narrate it play-by-play.

Moment analysis (the ONLY source of facts):
Game: {moment.get('game', '')}
Type: {moment.get('moment_type', '')}
What happened: {moment.get('what_happened', '')}
Setup: {moment.get('setup', '')}
Payoff: {moment.get('payoff', '')}
Reaction: {moment.get('reaction', '')}

Clip data:
{summary}

Style — {STYLE_GUIDE[style]}

Structure (follow the order, keep it uneven like real speech):
1. HOOK — {hook_line}
2. SETUP — one or two short lines of context
{pause_step}4. REACTION — your honest reaction to the turn
5. PAYOFF — land the outcome in your own words
6. optional tag line of 5 words or fewer ONLY if it feels natural (no "follow for more")
{fb}
Rules:
- {lo}-{hi} words total{pause_rule}
- Mention the streamer ({broadcaster or 'if known'}) and the game at most once each, naturally
- Use ONLY facts and numbers from the data above; never invent kills, scores, ranks or names
- Avoid these cliches entirely: "you won't believe", "nobody expected", "this insane moment",
  "absolutely incredible", "let's take a look", "watch what happens", "crazy gaming moment",
  "pure chaos", "losing his mind", "classic <name> energy", and any sign-off that just praises the clip
- Never repeat a catchphrase or quote from the clip title more than once, and don't spoil it in line 1
- Spoken words only: never describe what is on screen or write stage directions ("the banner flashes",
  "he smirks"); the viewer already sees it
- Never call him/her "solo" or "alone", and never add teammates, odds or stakes, unless the data above says so
- Plain text only: no labels, no stage directions, no emojis, no hashtags

Return ONLY the script, one thought per line."""


_SHORT_RULES = """- Do not repeat what the streamer says (see the clip data) and never put words in anyone's mouth
- Use ONLY facts and numbers from the data above; never invent kills, scores, ranks, names, or crowd/chat reactions
- Avoid cliches: "you won't believe", "nobody expected", "this insane moment", "absolutely incredible",
  "let's take a look", "watch what happens", "pure chaos", "losing his mind", "classic <name> energy"
- Spoken words only: never describe what is on screen or write stage directions ("the banner flashes",
  "he smirks"); the viewer already sees it
- Never call him/her "solo" or "alone", and never add teammates, odds or stakes, unless the data above says so
- Plain text only: no labels, no stage directions, no emojis, no hashtags"""


def _build_short_prompt(moment, hook, summary, style, lo, hi, feedback):
    hook_line = (f'Use this exact hook as line 1: "{hook["text"]}"'
                 if hook else "A strong first line (no greeting), max 12 words.")
    fb = f"\nA reviewer rejected the previous draft. Fix this: {feedback}\n" if feedback else ""
    return f"""You are a gaming creator adding a SHORT voice-over to a Twitch highlight. The clip KEEPS its own
game and streamer audio — you are not narrating it, you are adding a few well-placed lines around it.

Moment analysis (the ONLY source of facts):
Game: {moment.get('game', '')}
Type: {moment.get('moment_type', '')}
What happened: {moment.get('what_happened', '')}
Setup: {moment.get('setup', '')}
Payoff: {moment.get('payoff', '')}
Reaction: {moment.get('reaction', '')}

Clip data (may include what the streamer actually says):
{summary}

Style — {STYLE_GUIDE[style]}

Write at most 3 lines, one per line, in this order:
1. HOOK — {hook_line}
2. SETUP — one line, max 14 words: context the viewer can't see or hear yet. It plays BEFORE the
   payoff, so never give the payoff away.
3. TAG — optional, max 7 words, plays AFTER the payoff: a dry reaction or one-liner. Skip it if
   nothing genuinely fits; a silent ending is better than filler.
{fb}
Rules:
- {lo}-{hi} words in total
{_SHORT_RULES}

Return ONLY the lines."""


def _local_issues(script, moment, hook, summary, recent_scripts, lo, hi):
    issues = []
    words = word_count(script.replace("...", " "))
    if words < lo * 0.85:
        issues.append(f"too short ({words} words, need {lo}-{hi})")
    elif words > hi * 1.15:
        issues.append(f"too long ({words} words, need {lo}-{hi})")
    rep = repeated_ai_phrases(script, recent_scripts)
    if rep:
        issues.append("overused AI-sounding phrases: " + ", ".join(rep))
    bad = ungrounded_numbers(script, grounding_text(moment, summary, hook))
    if bad:
        issues.append("numbers not in the clip data: " + ", ".join(bad))
    claims = ungrounded_claims(script, grounding_text(moment, summary, hook))
    if claims:
        issues.append("claims not in the clip data: " + ", ".join(claims))
    stage = stage_directions(script)
    if stage:
        issues.append("describes the screen instead of speaking (" + ", ".join(stage) + ")")
    return issues


def generate_commentary(moment, hook, summary, structured=None, duration=None,
                        recent_scripts=None, feedback=None, max_local_retries=2, mode="full"):
    """Returns (script, local_issues). `local_issues` is empty when the draft
    passed every local guard; otherwise it holds what was still wrong after
    retries (the caller / quality agent decides what to do). Raises only if
    the LLM is unavailable for the very first draft."""
    from agents.model_invoke_agent_english import safe_invoke

    style = style_for(moment)
    lo, hi = target_word_range(duration, mode)
    local_fb = feedback
    script, issues = "", []
    for attempt in range(max_local_retries + 1):
        prompt = _build_prompt(moment, hook, summary, structured, style, lo, hi, local_fb, mode)
        script = clean_script(safe_invoke(prompt).content)
        if hook and hook.get("text") and not script.lower().startswith(hook["text"].lower()[:18]):
            script = hook["text"] + "\n" + script  # keep the scored hook as line 1
        issues = _local_issues(script, moment, hook, summary, recent_scripts, lo, hi)
        if not issues:
            break
        local_fb = "; ".join(issues) + (f". Also: {feedback}" if feedback else "")
        print(f"Commentary draft {attempt + 1} rejected locally: {local_fb}")
    return script, issues
