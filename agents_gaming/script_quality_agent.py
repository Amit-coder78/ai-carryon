# agents_gaming/script_quality_agent.py
"""
Gaming V2 Phase 5 — a script is only good enough to voice if hook strength,
naturalness, originality, accuracy and moment relevance are ALL above the
threshold (spec: > 7). Otherwise the caller regenerates it with this agent's
feedback; a weak script is never uploaded just because generation succeeded.

Two layers:
  1. Deterministic checks (no LLM): invented numbers cap accuracy, repeated
     AI phrases cap naturalness, near-duplicate of a recent script caps
     originality. These cannot be talked around by a lenient judge.
  2. LLM judge: scores the five dimensions plus pacing / payoff / repetition
     / AI-pattern notes.

If the judge call itself fails (provider outage, unparsable reply) the script
is flagged `unjudged` and does NOT pass (fail closed) — an unreviewed script
is never published; the scheduler just skips this cycle and the next cron run
tries again. GAMING_ALLOW_UNJUDGED=1 restores the old fail-open behaviour.
"""
import difflib
import os

from agents_gaming.commentary_agent import grounding_text
from agents_gaming.v2_utils import (
    clamp, extract_json, repeated_ai_phrases, ungrounded_numbers,
)

# Spec Phase 5: every score must be > 7. Env-tunable (e.g. 6.5 while tuning).
MIN_SCORE = float(os.getenv("GAMING_SCRIPT_MIN_SCORE", "7"))
SCORE_KEYS = ("hook", "naturalness", "originality", "accuracy", "moment_relevance")
# Pass rule. "balanced" (default): the things that make a Short WRONG or unwatchable are hard
# gates (hook, accuracy, moment relevance); naturalness / originality only need a basic floor,
# and the average must be decent. "strict" = the old rule (every score > MIN_SCORE).
RULE = os.getenv("GAMING_SCRIPT_RULE", "balanced").lower()
HARD_MIN = {"hook": float(os.getenv("GAMING_HOOK_MIN", "6")),
            "accuracy": float(os.getenv("GAMING_ACCURACY_MIN", "7")),
            "moment_relevance": float(os.getenv("GAMING_RELEVANCE_MIN", "7"))}
SOFT_FLOOR = float(os.getenv("GAMING_SOFT_FLOOR", "4"))      # naturalness / originality must be > this
AVG_MIN = float(os.getenv("GAMING_SCRIPT_AVG_MIN", "6"))


def average_score(scores):
    vals = [v for v in (scores or {}).values() if v is not None]
    return sum(vals) / len(vals) if vals else 0.0
SIMILARITY_LIMIT = 0.6
# The judge must score on ONE scale. The router's Groq budget runs out mid-run
# and silently hands later calls to Gemini, which scores far harsher — so the
# same script could pass or fail depending on call order. Pin the judge to
# Gemini (set GAMING_JUDGE_PROVIDER=router to use the normal router again).
JUDGE_PROVIDER = os.getenv("GAMING_JUDGE_PROVIDER", "gemini").lower()
# The router's shared Gemini timeout (15s) is too short for a thinking model
# reading a long judging prompt: three timeouts in a row trip the circuit
# breaker and kill Gemini for the whole run. The judge gets its own budget.
JUDGE_TIMEOUT_SECONDS = int(os.getenv("GAMING_JUDGE_TIMEOUT_SECONDS", "45"))
# If the judge can't produce scores (outage / unparsable reply) the script is
# UNJUDGED. Default: do not publish it. Set GAMING_ALLOW_UNJUDGED=1 to opt out.
ALLOW_UNJUDGED = os.getenv("GAMING_ALLOW_UNJUDGED", "0") == "1"
JUDGE_PARSE_RETRIES = 1


# The judge must stay on ONE scale. A Gemini timeout used to hand the judging to the router (Groq),
# which scores far more leniently, so the same script could pass or fail depending on which judge
# happened to answer. Default: retry Gemini, and if it still fails treat the script as UNJUDGED
# (the scheduler then skips the cycle and keeps its saved work) instead of judging on another scale.
# GAMING_JUDGE_ROUTER_FALLBACK=1 restores the old fall-back-to-router behaviour.
JUDGE_ROUTER_FALLBACK = os.getenv("GAMING_JUDGE_ROUTER_FALLBACK", "0") == "1"
JUDGE_GEMINI_RETRIES = int(os.getenv("GAMING_JUDGE_GEMINI_RETRIES", "1"))
# Optional: judge with a different Gemini model (free-tier daily quota is counted per project AND per model).
JUDGE_MODEL = os.getenv("GAMING_JUDGE_MODEL", "").strip()
_QUOTA_MARKERS = ("resource_exhausted", "429", "quota")


def _short(err, limit=160):
    """One short line instead of a 2,000-character API error dump in the logs."""
    text = " ".join(str(err).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _invoke_judge(prompt):
    import agents.model_invoke_agent_english as llm
    if JUDGE_PROVIDER == "gemini":
        err = "circuit breaker open"
        if not llm._provider_dead("gemini"):
            for n in range(1 + JUDGE_GEMINI_RETRIES):
                print("[gaming judge] Trying Gemini" + (f" (retry {n})" if n else ""))
                resp, err = llm._run_with_timeout(
                    lambda: (llm._get_gemini(JUDGE_MODEL) if JUDGE_MODEL else llm._get_gemini()).invoke(prompt),
                    JUDGE_TIMEOUT_SECONDS)
                llm._record_outcome("gemini", resp is not None)
                if resp is not None:
                    return llm._normalize_response(resp)
                print(f"[gaming judge] Gemini failed: {_short(err)}")
                if any(m in str(err).lower() for m in _QUOTA_MARKERS):
                    print("[gaming judge] Gemini quota used up - a retry cannot help, not retrying")
                    break
                if llm._provider_dead("gemini"):
                    break
        if not JUDGE_ROUTER_FALLBACK:
            raise RuntimeError(f"Gemini judge unavailable ({_short(err)}); not judging on a different model")
        print("[gaming judge] falling back to the router (GAMING_JUDGE_ROUTER_FALLBACK=1)")
    return llm.safe_invoke(prompt)


def _similarity(script, recent_scripts):
    best = 0.0
    for r in recent_scripts or []:
        best = max(best, difflib.SequenceMatcher(None, script.lower(), r.lower()).ratio())
    return best


def _judge_prompt(script, moment, hook, summary, mode="full"):
    overlay = ("\nNOTE: this is a SHORT overlay (a hook plus one or two lines) that plays over the clip's own "
               "game/streamer audio, not a full voiceover. Do NOT penalise brevity or silence; DO penalise filler, "
               "cliches, repeating what the streamer already says, and any invented detail.\n"
               if mode == "short" else "")
    return f"""You are a strict editor for a gaming YouTube Shorts channel. Judge this voiceover
script, which plays over real gameplay.

Moment analysis (ground truth):
Game: {moment.get('game', '')} | Type: {moment.get('moment_type', '')}
What happened: {moment.get('what_happened', '')}
Setup: {moment.get('setup', '')} | Payoff: {moment.get('payoff', '')}
Clip data:
{summary}
{overlay}
Script:
\"\"\"
{script}
\"\"\"

Score 1-10 (be harsh; 8+ means a human creator could have said it):
- hook: does the first line stop a scroll without giving the payoff away?
- naturalness: sounds like a person talking, not an AI announcer (varied rhythm, no cliches)
- originality: a specific angle on THIS moment, not a generic hype template
- accuracy: every claim is supported by the moment analysis / clip data
- moment_relevance: it is about what actually happens in the clip
Also list concrete problems (pacing, weak payoff, repetition, AI-sounding lines).

Reply with ONLY this JSON:
{{"hook": 0, "naturalness": 0, "originality": 0, "accuracy": 0, "moment_relevance": 0,
  "issues": ["..."], "fix": "one sentence telling the writer what to change"}}"""


def evaluate_script(script, moment, hook=None, summary="", recent_scripts=None, mode="full"):
    """Returns {'passed': bool, 'scores': {...}, 'issues': [...], 'feedback': str,
    'judge_skipped': bool}."""
    scores = {k: None for k in SCORE_KEYS}
    issues, fix = [], ""
    judge_skipped = False

    try:
        data = {}
        for _ in range(1 + JUDGE_PARSE_RETRIES):          # retry only an unparsable reply
            raw = _invoke_judge(_judge_prompt(script, moment, hook, summary, mode)).content
            data = extract_json(raw) or {}
            for k in SCORE_KEYS:
                scores[k] = clamp(data.get(k), 1, 10, default=None)
            if any(v is not None for v in scores.values()):
                break
        if all(v is None for v in scores.values()):
            judge_skipped = True
        issues = [str(i) for i in data.get("issues", [])] if isinstance(data.get("issues"), list) else []
        fix = str(data.get("fix", "")).strip()
    except Exception as e:
        print(f"Script quality: judge unavailable ({e}) — deterministic checks only")
        judge_skipped = True

    # Deterministic layer — caps the judge's scores.
    bad_nums = ungrounded_numbers(script, grounding_text(moment, summary, hook))
    if bad_nums:
        scores["accuracy"] = min(scores["accuracy"] if scores["accuracy"] is not None else 10, 4)
        issues.append("states numbers not in the clip data: " + ", ".join(bad_nums))
    capped = set()
    if bad_nums:
        capped.add("accuracy")
    rep = repeated_ai_phrases(script, recent_scripts)
    if rep:
        capped.add("naturalness")
        scores["naturalness"] = min(scores["naturalness"] if scores["naturalness"] is not None else 10, 5)
        issues.append("overused AI-sounding phrases: " + ", ".join(rep))
    sim = _similarity(script, recent_scripts)
    if sim >= SIMILARITY_LIMIT:
        capped.add("originality")
        scores["originality"] = min(scores["originality"] if scores["originality"] is not None else 10, 4)
        issues.append(f"too similar to a recent script ({sim:.0%})")

    # Unscored dimensions (judge skipped) pass; scored ones must be > MIN_SCORE.
    if RULE == "strict":
        failing = [k for k, v in scores.items() if v is not None and not v > MIN_SCORE]
    else:
        failing = []
        for k, v in scores.items():
            if v is None:
                continue
            below = (v < HARD_MIN[k]) if k in HARD_MIN else (not v > SOFT_FLOOR)
            # A deterministic problem (invented number, overused AI phrase, copy of a recent
            # script) is never excused by the average.
            if below or k in capped:
                failing.append(k)
        if (not failing and any(v is not None for v in scores.values())
                and average_score(scores) < AVG_MIN):
            failing = [k for k, v in scores.items() if v is not None and v < AVG_MIN] or ["average"]
    passed = not failing
    if judge_skipped and not ALLOW_UNJUDGED:
        # Never publish a script nobody reviewed just because the reviewer was down.
        passed = False
        issues.append("quality judge unavailable — script was not reviewed")
    feedback = fix or ("; ".join(issues) if issues else "")
    if failing and not feedback:
        feedback = "raise: " + ", ".join(failing)

    return {
        "passed": passed,
        "scores": scores,
        "failing": failing,
        "issues": issues,
        "feedback": feedback,
        "judge_skipped": judge_skipped,
        "unjudged": judge_skipped,
    }
