# agents_gaming/script_agent.py
from dotenv import load_dotenv
load_dotenv()

# Reuses the same Groq-first/Gemini-fallback invoke helper as the English
# channel — gaming scripts are English, same as agents/script_agent.py.
from agents.model_invoke_agent_english import safe_invoke


def create_gaming_script(clip_summary, structured=None):
    """One-clip hype/commentary script, ~20-35s of speech (roughly 55-90
    words at the pace used in agents/voice_agent.py). Written to sit OVER
    the real Twitch clip footage, not narrate a static image."""
    structured = structured or {}
    game = structured.get("game", "")
    broadcaster = structured.get("broadcaster", "")

    context_line = ""
    if broadcaster:
        context_line = f"This clip is from streamer {broadcaster}"
        if game:
            context_line += f" playing {game}."
        else:
            context_line += "."

    prompt = f"""You are a hype gaming commentator writing a YouTube Shorts voiceover
script that plays OVER a real gameplay clip (the viewer is watching the clip
while you talk, not looking at you).

Clip data:
{clip_summary}
{context_line}

STRICT RULES:
- 20-35 seconds of speech — total word count: 55-90 words
- First line is a HOOK about what's about to happen in the clip — specific to
  THIS clip; avoid stock openers like "Watch what happens", "You won't believe",
  "Nobody expected" or "Let's take a look"
- Do NOT describe the clip play-by-play like a boring recap — react to it
  like a hype gaming commentator would, short punchy sentences
- Mention the streamer's name and the game naturally, once each
- Last line: a short call to follow for more gaming clips
- Plain text only — no stage directions, no labels like "Hook:", no emojis,
  no hashtags

Return ONLY the script text, nothing else."""

    response = safe_invoke(prompt)
    script = response.content.strip()

    words = script.split()
    for _ in range(2):
        if len(words) >= 55:
            break
        prompt2 = (
            f"This script is only {len(words)} words. Expand it to ~75 words.\n"
            f"Keep the same hook and single-clip focus — do not add a second clip "
            f"or a full recap.\n\nScript:\n{script}"
        )
        script = safe_invoke(prompt2).content.strip()
        words = script.split()

    if len(words) > 90:
        script = " ".join(words[:90])

    return script


# ── Gaming V2 (Phases 3-5): hook -> commentary -> quality gate ──────────────

import os
import json

RECENT_SCRIPTS_KEY = "gaming_recent_scripts"
RECENT_HOOKS_KEY = "gaming_recent_hook_types"
MAX_SCRIPT_ATTEMPTS = 3
# After attempt 2, stop if even the best draft averages below this: the clip is a lost cause
# and every further attempt just burns Groq / Gemini calls.
EARLY_STOP_AVG = float(os.getenv("GAMING_EARLY_STOP_AVG", "5"))


def _load_recent(db, key, limit):
    try:
        raw = db.get_meta(key)
        return json.loads(raw)[-limit:] if raw else []
    except Exception:
        return []


def remember_script(db, script, hook_type=None):
    """Keep the last few scripts/hook types so later runs can detect repeated
    phrasing and hook types (bounded; stored in gaming_meta)."""
    try:
        scripts = _load_recent(db, RECENT_SCRIPTS_KEY, 9) + [script]
        db.set_meta(RECENT_SCRIPTS_KEY, json.dumps(scripts[-10:]))
        if hook_type:
            hooks = _load_recent(db, RECENT_HOOKS_KEY, 9) + [hook_type]
            db.set_meta(RECENT_HOOKS_KEY, json.dumps(hooks[-10:]))
    except Exception as e:
        print(f"Could not store recent script memory: {e}")


# Last resort when every narrated draft fails the quality bar: publish the clip with ONLY the hook
# (the streamer's own audio tells the story). The hook is judged on its own and must be accurate.
# Off by default here; the gaming workflow turns it on with GAMING_HOOK_ONLY_FALLBACK=1.
HOOK_ONLY_FALLBACK = os.getenv("GAMING_HOOK_ONLY_FALLBACK", "0") == "1"
HOOK_ONLY_MIN_ACCURACY = float(os.getenv("GAMING_HOOK_ONLY_MIN_ACCURACY", "7"))
HOOK_ONLY_MAX_TRIES = int(os.getenv("GAMING_HOOK_ONLY_MAX_TRIES", "2"))


def _hook_only_fallback(hook_hist, moment, summary, recent_scripts, mode):
    """Try the most accurate hooks seen in this run as a one-line script.
    Returns a passed result, an {"unjudged": True} result when no judge is available, or None."""
    from agents_gaming.script_quality_agent import evaluate_script
    ranked = sorted(
        (h for h in hook_hist if (h.get("hook") or {}).get("text")),
        key=lambda h: (h.get("accuracy") is not None, h.get("accuracy") or 0,
                       (h["hook"].get("score") or 0)),
        reverse=True)
    seen, tried = set(), 0
    for h in ranked:
        text = h["hook"]["text"].strip()
        if text.lower() in seen:
            continue
        if tried >= HOOK_ONLY_MAX_TRIES:
            break
        seen.add(text.lower())
        tried += 1
        quality = evaluate_script(text, moment, h["hook"], summary, recent_scripts, mode=mode)
        if quality.get("unjudged"):
            print("Hook-only fallback: judge unavailable - not publishing an unreviewed hook")
            return {"script": text, "hook": h["hook"], "quality": quality, "attempts": tried,
                    "passed": False, "fallback": False, "unjudged": True}
        accuracy = (quality.get("scores") or {}).get("accuracy")
        print(f"Hook-only check {tried}: '{text}' accuracy={accuracy} (needs >= {HOOK_ONLY_MIN_ACCURACY})")
        if accuracy is not None and accuracy >= HOOK_ONLY_MIN_ACCURACY:
            print("Hook-only fallback: publishing the clip with just the hook")
            return {"script": text, "hook": h["hook"], "quality": quality, "attempts": tried,
                    "passed": True, "fallback": False, "hook_only": True}
    return None


def create_gaming_script_v2(clip, moment, summary, structured, db=None, mode="full", state=None):
    """V2 script pipeline for one analysed clip.

    Returns a dict:
      {"script", "hook", "quality", "attempts", "passed", "fallback"}
    `passed` is False when every attempt failed the quality bar — the caller
    must NOT upload that script. If hook/commentary generation breaks outright
    (LLM outage), falls back to the legacy single-prompt script (`fallback`=True).
    """
    from agents_gaming.hook_agent import generate_hooks
    from agents_gaming.commentary_agent import generate_commentary
    from agents_gaming.script_quality_agent import evaluate_script, average_score

    recent_scripts = _load_recent(db, RECENT_SCRIPTS_KEY, 5) if db else []
    recent_hooks = _load_recent(db, RECENT_HOOKS_KEY, 3) if db else []
    duration = clip.get("duration")

    # Resume support: progress for this clip is saved after every step, so a run
    # that stopped (rate limit, judge outage) continues from the same hook / draft /
    # attempt number instead of paying for them again.
    pkey = f"script:{clip.get('id')}"
    saved = (state.get(pkey) if state is not None else None) or {}

    def _save(**kw):
        if state is not None:
            saved.update(kw)
            state.set(pkey, dict(saved))

    if saved.get("hook_done"):
        hook = saved.get("hook")
        print(f"Resuming saved hook: {hook['text'] if hook else None}")
    else:
        hook = generate_hooks(moment, summary, recent_hook_types=recent_hooks)
        _save(hook=hook, hook_done=True, attempt=1, feedback=None,
              used_hooks=[hook["text"]] if hook else [], draft=None)
        if hook:
            print(f"Hook [{hook['type']} {hook['score']}]: {hook['text']}")

    feedback, last, best = saved.get("feedback"), None, None
    used_hooks = list(saved.get("used_hooks") or ([hook["text"]] if hook else []))
    hook_hist = list(saved.get("hook_hist") or [])      # [{"hook": {...}, "accuracy": n}] for the hook-only fallback

    def _give_up(result):
        if HOOK_ONLY_FALLBACK:
            alt = _hook_only_fallback(hook_hist, moment, summary, recent_scripts, mode)
            if alt:
                return alt
        return result

    first_attempt = int(saved.get("attempt") or 1)
    for attempt in range(first_attempt, MAX_SCRIPT_ATTEMPTS + 1):
        try:
            if saved.get("draft") and saved.get("attempt") == attempt:
                script, local_issues = saved["draft"]["script"], saved["draft"]["local_issues"]
                print(f"Resuming saved draft for attempt {attempt} — judging it without rewriting")
            else:
                script, local_issues = generate_commentary(
                    moment, hook, summary, structured, duration=duration,
                    recent_scripts=recent_scripts, feedback=feedback, mode=mode,
                )
                _save(attempt=attempt, draft={"script": script, "local_issues": list(local_issues or [])})
        except Exception as e:
            print(f"Commentary generation failed ({e}) — falling back to legacy script")
            return {"script": create_gaming_script(summary, structured), "hook": hook,
                    "quality": None, "attempts": attempt, "passed": True, "fallback": True}

        quality = evaluate_script(script, moment, hook, summary, recent_scripts, mode=mode)
        if local_issues:  # a draft that still tripped a local guard can't pass
            quality["passed"] = False
            quality["issues"] = quality["issues"] + local_issues
        if quality.get("unjudged") and not quality["passed"]:
            print("Quality judge unavailable — not publishing an unreviewed script this cycle")
            return {"script": script, "hook": hook, "quality": quality, "attempts": attempt,
                    "passed": False, "fallback": False, "unjudged": True}
        s = quality["scores"]
        print(f"Script attempt {attempt}: passed={quality['passed']} scores={s} issues={quality['issues']}")
        if hook:
            hook_hist.append({"hook": {k: hook.get(k) for k in ("type", "text", "score")},
                              "accuracy": s.get("accuracy")})
            _save(hook_hist=list(hook_hist))
        cand = {"script": script, "hook": hook, "quality": quality, "attempts": attempt,
                "passed": quality["passed"], "fallback": False}
        if quality["passed"]:
            return cand
        last = cand
        if best is None or average_score(quality["scores"]) > average_score(best["quality"]["scores"]):
            best = cand                      # keep the strongest draft, not just the latest
        if attempt >= 2 and average_score(best["quality"]["scores"]) < EARLY_STOP_AVG:
            print(f"Best draft averages {average_score(best['quality']['scores']):.1f} "
                  f"(< {EARLY_STOP_AVG}) — stopping early to save LLM calls")
            return _give_up(best)
        feedback = quality["feedback"] or "; ".join(quality["issues"])
        _save(attempt=attempt + 1, draft=None, feedback=feedback, hook=hook, used_hooks=used_hooks)
        # The hook is generated once and forced in as line 1, so rewriting the
        # body can never raise a failing hook score — redo the hook itself.
        if attempt < MAX_SCRIPT_ATTEMPTS and "hook" in (quality.get("failing") or []):
            new_hook = generate_hooks(moment, summary, recent_hook_types=recent_hooks,
                                      feedback=feedback, avoid=used_hooks)
            if new_hook:
                hook = new_hook
                used_hooks.append(hook["text"])
                _save(hook=hook, used_hooks=used_hooks)
                print(f"New hook [{hook['type']} {hook['score']}]: {hook['text']}")

    final = dict(best or last)          # every attempt failed the bar -> passed=False (best draft, for logging)
    final["attempts"] = MAX_SCRIPT_ATTEMPTS
    return _give_up(final)
