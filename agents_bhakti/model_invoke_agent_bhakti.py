# agents_bhakti/model_invoke_agent_bhakti.py
"""
Shared LLM invocation router for the Bhakti (devotional) pipeline.

Third independent twin of agents/model_invoke_agent_english.py and
agents_hindi/model_invoke_agent_hindi.py. Kept separate on purpose: the
Bhakti channel runs as its own process/service with its own Groq/Gemini
call budget, so a burst of calls (or a dead provider) on one channel can
never eat into another channel's quota or trip its circuit breaker.

Same budget/circuit-breaker design as the other two routers:
- a per-run Groq call counter that proactively switches to Gemini once
  GROQ_MAX_CALLS_PER_RUN is hit (instead of waiting for a real 429), and
- a circuit breaker that marks a provider "dead" for the rest of the run
  after FAILURE_THRESHOLD consecutive failures, so later calls skip it
  immediately instead of eating its timeout every time.
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

GROQ_MAX_CALLS_PER_RUN = int(os.environ.get("GROQ_MAX_CALLS_PER_RUN", "8"))
# Extra Groq calls allowed on the SHARED key once the personal budget is spent (0 = off).
GROQ_SHARED_EXTRA_CALLS = int(os.environ.get("GROQ_SHARED_EXTRA_CALLS", "8"))
GROQ_TIMEOUT_SECONDS = int(os.environ.get("GROQ_TIMEOUT_SECONDS", "15"))
GEMINI_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", "15"))
FAILURE_THRESHOLD = int(os.environ.get("LLM_CIRCUIT_BREAKER_THRESHOLD", "3"))

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
        f"[llm_router:bhakti] Reset for new run — Groq budget: {GROQ_MAX_CALLS_PER_RUN} calls, "
        f"circuit breaker trips after {FAILURE_THRESHOLD} consecutive fails per provider"
    )


def groq_calls_used():
    with _lock:
        return _state["groq_call_count"]


def provider_status():
    with _lock:
        return dict(_state)


def _get_groq(model="openai/gpt-oss-120b", temperature=None):
    from langchain_groq import ChatGroq
    kwargs = dict(
        model=model,
        groq_api_key=os.getenv("GROQ_API_KEY"),
        max_retries=0,
    )
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatGroq(**kwargs)


def _get_gemini(model="gemini-3.5-flash"):
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(model=model, google_api_key=os.getenv("GEMINI_API_KEY"))


def _run_with_timeout(fn, timeout):
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
                    f"[llm_router:bhakti] {provider} failed {FAILURE_THRESHOLD}x in a row this run "
                    f"— marking it DEAD for the rest of the run. {other} will carry on alone."
                )


def _try_groq(prompt, groq_model, temperature):
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
            print(f"[llm_router:bhakti] Groq budget on personal key spent — +{GROQ_SHARED_EXTRA_CALLS} calls on shared key")
    if not under_budget:
        print(f"[llm_router:bhakti] Groq budget exhausted ({limit}/run) — skipping to Gemini")
        return None

    with _lock:
        _state["groq_call_count"] += 1
        count_now = _state["groq_call_count"]
    print(f"[llm_router:bhakti] Trying Groq ({count_now}/{limit})")

    resp, err = _run_with_timeout(
        lambda: _get_groq(groq_model, temperature).invoke(prompt), GROQ_TIMEOUT_SECONDS
    )
    _record_outcome("groq", resp is not None)
    if resp is None:
        print(f"[llm_router:bhakti] Groq failed: {err}")
    return _normalize_response(resp)


def _try_gemini(prompt):
    print("[llm_router:bhakti] Trying Gemini")
    resp, err = _run_with_timeout(lambda: _get_gemini().invoke(prompt), GEMINI_TIMEOUT_SECONDS)
    _record_outcome("gemini", resp is not None)
    if resp is None:
        print(f"[llm_router:bhakti] Gemini failed: {err}")
    return _normalize_response(resp)


def _normalize_response(resp):
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
            pass
    return resp


def safe_invoke(prompt, groq_model="openai/gpt-oss-120b", temperature=None):
    groq_dead = _provider_dead("groq")
    gemini_dead = _provider_dead("gemini")

    if groq_dead and gemini_dead:
        print(
            "[llm_router:bhakti] Both Groq and Gemini are dead this run — "
            f"last-ditch attempt with Groq {LAST_RESORT_GROQ_MODEL}"
        )
        resp, err = _run_with_timeout(
            lambda: _get_groq(LAST_RESORT_GROQ_MODEL, temperature).invoke(prompt),
            GROQ_TIMEOUT_SECONDS,
        )
        if resp is not None:
            return _normalize_response(resp)
        raise RuntimeError(
            f"[llm_router:bhakti] Both providers unavailable this run, last-ditch attempt also failed: {err}"
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
        print("[llm_router:bhakti] Groq marked dead this run — routing directly to Gemini")
        resp = _try_gemini(prompt)
        if resp is not None:
            return _normalize_response(resp)
    print(f"[llm_router:bhakti] Primary paths failed for this call — last resort: Groq {LAST_RESORT_GROQ_MODEL}")
    resp, err = _run_with_timeout(
        lambda: _get_groq(LAST_RESORT_GROQ_MODEL, temperature).invoke(prompt),
        GROQ_TIMEOUT_SECONDS,
    )
    if resp is not None:
        return _normalize_response(resp)
    raise RuntimeError(f"[llm_router:bhakti] All LLM providers failed for this call: {err}")
