# agents/model_invoke_agent_english.py
"""
Shared LLM invocation router for the English pipeline.

This is the English-side twin of agents_hindi/model_invoke_agent_hindi.py.
Kept as a separate module (not shared code) on purpose: the two channels run
as independent processes/services with independent Groq/Gemini quotas, so
each needs its own independent call budget and circuit-breaker state — one
channel hitting its rate limit or losing a provider must never affect the
other channel's counters.

Problem it solves:
- Every agent (script, seo, ab_title, flow_prompt, research, thumbnail,
  image, manim, comment_reply, trending, ...) used to define its own
  safe_invoke()/get_llm() with a 20s-timeout-then-Gemini fallback, or in a
  few cases (comment_reply_agent, trending_agent, manim_agent) had NO
  fallback at all. None of them knew how many Groq calls had already
  happened this run, and ChatGroq's own SDK was retrying 429s internally
  for 5-9+ seconds each time, burning minutes before ever falling back.
- One video generation run makes 10-15+ LLM calls (trending, saturation,
  comparison, research, script + retries, 3x A/B titles + scoring, SEO,
  thumbnail text, flow prompts). On a free/low-tier Groq plan that alone
  can exceed the requests-per-minute limit.

Fix, part 1 — proactive rate-limit avoidance:
- A single process-wide counter tracks Groq calls made so far *this run*.
- Once GROQ_MAX_CALLS_PER_RUN is hit, remaining calls skip Groq entirely and
  go straight to Gemini — proactively, before the real 429 wall.
- ChatGroq is created with max_retries=0 so a 429 fails fast (instead of the
  SDK silently backing off for 5-9s per call) and control returns to the
  router immediately to decide what happens next.

Fix, part 2 — circuit breaker for a fully-dead provider:
- If Groq isn't just rate-limited but genuinely down/broken (bad key, outage,
  quota fully exhausted for the day), every call would otherwise still try
  Groq first, wait out its timeout, and only then fall back — wasting the
  timeout on every single call for the rest of the run.
- Instead: after LLM_CIRCUIT_BREAKER_THRESHOLD consecutive failures on a
  provider, that provider is marked "dead" for the rest of the run and is
  skipped entirely — the other provider carries the whole run from then on.
- Works both directions: Groq dead -> Gemini carries on. Gemini dead ->
  Groq carries on (budget/circuit permitting). Only if BOTH are dead does
  the router fall to a last-ditch smaller Groq model, and only raises if
  that also fails.
"""

import os
import threading

_lock = threading.Lock()

_state = {
    "groq_call_count": 0,
    "groq_consecutive_fail": 0,
    "gemini_consecutive_fail": 0,
    "groq_dead": False,
    "gemini_dead": False,
}

# How many Groq calls to allow per scheduler run before proactively switching
# to Gemini for the rest of the run. Tune this to your Groq plan's RPM limit.
GROQ_MAX_CALLS_PER_RUN = int(os.environ.get("GROQ_MAX_CALLS_PER_RUN", "8"))
# Extra Groq calls allowed on the SHARED key once the personal budget is spent (0 = off).
GROQ_SHARED_EXTRA_CALLS = int(os.environ.get("GROQ_SHARED_EXTRA_CALLS", "8"))

# How long to wait for a single call before giving up on it.
GROQ_TIMEOUT_SECONDS = int(os.environ.get("GROQ_TIMEOUT_SECONDS", "15"))
GEMINI_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", "15"))

# How many consecutive failures on one provider before it's marked dead
# for the rest of the run (circuit breaker).
FAILURE_THRESHOLD = int(os.environ.get("LLM_CIRCUIT_BREAKER_THRESHOLD", "3"))

# Last-resort model when everything else has failed — kept as its own,
# separate quota bucket from the main "openai/gpt-oss-120b" model.
LAST_RESORT_GROQ_MODEL = "openai/gpt-oss-20b"


def reset_groq_budget():
    """Call once at the very start of each scheduler run (main entrypoint)."""
    with _lock:
        _state["groq_call_count"] = 0
        _state["groq_consecutive_fail"] = 0
        _state["gemini_consecutive_fail"] = 0
        _state["groq_dead"] = False
        _state["gemini_dead"] = False
        _state["groq_on_shared"] = False
    print(
        f"[llm_router:english] Reset for new run — Groq budget: {GROQ_MAX_CALLS_PER_RUN} calls, "
        f"circuit breaker trips after {FAILURE_THRESHOLD} consecutive fails per provider"
    )


def groq_calls_used():
    with _lock:
        return _state["groq_call_count"]


def provider_status():
    """Snapshot for logging/debugging."""
    with _lock:
        return dict(_state)


def _get_groq(model="openai/gpt-oss-120b", temperature=None):
    from langchain_groq import ChatGroq
    kwargs = dict(
        model=model,
        groq_api_key=os.getenv("GROQ_API_KEY"),
        max_retries=0,  # fail fast — the router decides what happens next, not the SDK
    )
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatGroq(**kwargs)


def _get_gemini(model="gemini-3.5-flash"):
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(model=model, google_api_key=os.getenv("GEMINI_API_KEY"))


def _run_with_timeout(fn, timeout):
    """Run fn() in a daemon thread with a hard timeout. Returns (result, error)."""
    result = [None]
    error = [None]

    def _target():
        try:
            result[0] = fn()
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if result[0] is None and error[0] is None:
        error[0] = TimeoutError(f"timed out after {timeout}s")
    return result[0], error[0]


def _provider_dead(provider):
    with _lock:
        return _state[f"{provider}_dead"]


def _record_outcome(provider, success):
    """Update consecutive-failure count and trip the circuit breaker if needed."""
    with _lock:
        if success:
            _state[f"{provider}_consecutive_fail"] = 0
        else:
            _state[f"{provider}_consecutive_fail"] += 1
            if (
                _state[f"{provider}_consecutive_fail"] >= FAILURE_THRESHOLD
                and not _state[f"{provider}_dead"]
            ):
                _state[f"{provider}_dead"] = True
                other = "gemini" if provider == "groq" else "groq"
                print(
                    f"[llm_router:english] {provider} failed {FAILURE_THRESHOLD}x in a row this run "
                    f"— marking it DEAD for the rest of the run. {other} will carry on alone."
                )


def _try_groq(prompt, groq_model, temperature):
    """Attempt Groq once, respecting budget. Returns response or None."""
    with _lock:
        limit = GROQ_MAX_CALLS_PER_RUN + (GROQ_SHARED_EXTRA_CALLS if _state.get("groq_on_shared") else 0)
        under_budget = _state["groq_call_count"] < limit
    if not under_budget and not _state.get("groq_on_shared") and GROQ_SHARED_EXTRA_CALLS > 0:
        # Personal budget spent: carry on with the shared Groq key for a few more calls
        # instead of dropping straight to the weaker fallbacks.
        try:
            import key_fallback
            switched = key_fallback.switch_to_shared("groq", reason="personal Groq budget spent")
        except Exception:
            switched = False
        if switched:
            with _lock:
                _state["groq_on_shared"] = True
                limit = GROQ_MAX_CALLS_PER_RUN + GROQ_SHARED_EXTRA_CALLS
                under_budget = _state["groq_call_count"] < limit
            print(f"[llm_router:english] Groq budget on personal key spent — +{GROQ_SHARED_EXTRA_CALLS} calls on shared key")
    if not under_budget:
        print(f"[llm_router:english] Groq budget exhausted ({limit}/run) — skipping to Gemini")
        return None

    with _lock:
        _state["groq_call_count"] += 1
        count_now = _state["groq_call_count"]
    print(f"[llm_router:english] Trying Groq ({count_now}/{limit})")

    resp, err = _run_with_timeout(
        lambda: _get_groq(groq_model, temperature).invoke(prompt), GROQ_TIMEOUT_SECONDS
    )
    _record_outcome("groq", resp is not None)
    if resp is None:
        print(f"[llm_router:english] Groq failed: {err}")
    return _normalize_response(resp)
def _try_gemini(prompt):
    """Attempt Gemini once. Returns response or None."""
    print("[llm_router:english] Trying Gemini")
    resp, err = _run_with_timeout(lambda: _get_gemini().invoke(prompt), GEMINI_TIMEOUT_SECONDS)
    _record_outcome("gemini", resp is not None)
    if resp is None:
        print(f"[llm_router:english] Gemini failed: {err}")
    return _normalize_response(resp)

def _normalize_response(resp):
    """
    Ensure resp.content is always a plain string, regardless of which
    provider answered. Groq's responses are always a string. Some Gemini
    models (e.g. gemini-3.5-flash, which can include reasoning/thinking
    parts) can return .content as a list of parts instead — each part is
    either a plain string or a dict with a "text" key. Mutating in place
    (rather than wrapping) means every existing caller's
    `response.content.strip()` keeps working unchanged.
    """
    if resp is None:
        return resp
    content = getattr(resp, "content", None)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
        try:
            resp.content = "".join(parts)
        except Exception:
            # Some response objects may be immutable — fall back to
            # attaching the flattened text under a new attribute name
            # is pointless if callers only ever read .content, so at
            # least avoid crashing here; the caller-side .strip() will
            # still fail, but this is a very unlikely edge case.
            pass
    return resp

def safe_invoke(prompt, groq_model="openai/gpt-oss-120b", temperature=None):
    """
    Route one LLM call across Groq and Gemini with a circuit breaker.

    - Normal case: try Groq (budget-limited), fall back to Gemini on failure.
    - If Groq is marked dead (N consecutive failures this run), every call
      skips it entirely and goes straight to Gemini.
    - If Gemini is marked dead, every call skips it and relies on Groq
      (budget/circuit permitting).
    - If BOTH are dead, one last-ditch attempt with Groq's smaller model
      (separate quota bucket) before raising.
    """
    groq_dead = _provider_dead("groq")
    gemini_dead = _provider_dead("gemini")

    if groq_dead and gemini_dead:
        print(
            "[llm_router:english] Both Groq and Gemini are dead this run — "
            f"last-ditch attempt with Groq {LAST_RESORT_GROQ_MODEL}"
        )
        resp, err = _run_with_timeout(
            lambda: _get_groq(LAST_RESORT_GROQ_MODEL, temperature).invoke(prompt),
            GROQ_TIMEOUT_SECONDS,
        )
        if resp is not None:
            return _normalize_response(resp)
        raise RuntimeError(
            f"[llm_router:english] Both providers unavailable this run, last-ditch attempt also failed: {err}"
        )

    if not groq_dead:
        resp = _try_groq(prompt, groq_model, temperature)
        if resp is not None:
            return _normalize_response(resp)
        if not gemini_dead:
            resp = _try_gemini(prompt)
            if resp is not None:
                return _normalize_response(resp)
    else:
        print("[llm_router:english] Groq marked dead this run — routing directly to Gemini")
        resp = _try_gemini(prompt)
        if resp is not None:
            return _normalize_response(resp)
    print(f"[llm_router:english] Primary paths failed for this call — last resort: Groq {LAST_RESORT_GROQ_MODEL}")
    resp, err = _run_with_timeout(
        lambda: _get_groq(LAST_RESORT_GROQ_MODEL, temperature).invoke(prompt),
        GROQ_TIMEOUT_SECONDS,
    )
    if resp is not None:
        return _normalize_response(resp)
    raise RuntimeError(f"[llm_router:english] All LLM providers failed for this call: {err}")
