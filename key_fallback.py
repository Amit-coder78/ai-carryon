"""
Runtime API-key fallback: per-channel key first, shared key as a safety net.

Each channel workflow sets, for example:
    GROQ_API_KEY         = this channel's own key   (used normally)
    GROQ_API_KEY_SHARED  = the old shared key       (used only when needed)

install() wraps the HTTP layer (httpx for Groq/Gemini/Sarvam SDKs, requests for Pexels
and raw Gemini calls). If a call to api.groq.com / generativelanguage.googleapis.com /
api.pexels.com / api.sarvam.ai comes back 429 (rate limit), 402 (out of credits) or 401/403 (bad/revoked key), the same
request is retried once with the shared key. No call site needs to change.

- 429  -> keep using the shared key for COOLDOWN_429_SECONDS, then try personal again.
- 401/402/403 (or Gemini "API key not valid") -> use the shared key for the rest of the run.
  (402 = out of credits, e.g. Sarvam "No credits available".)
- No *_SHARED secret, or it equals the personal key -> this module does nothing.

Env: KEY_FALLBACK=0 disables it. KEY_FALLBACK_COOLDOWN_429 overrides the 60s cooldown.
"""
import os
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# provider -> (host, personal env var, shared env var)
PROVIDERS = {
    "groq": ("api.groq.com", "GROQ_API_KEY", "GROQ_API_KEY_SHARED"),
    "gemini": ("generativelanguage.googleapis.com", "GEMINI_API_KEY", "GEMINI_API_KEY_SHARED"),
    "pexels": ("api.pexels.com", "PEXELS_API_KEY", "PEXELS_API_KEY_SHARED"),
    "sarvam": ("api.sarvam.ai", "SARVAM_API_KEY", "SARVAM_API_KEY_SHARED"),
}
HOST_TO_PROVIDER = {v[0]: k for k, v in PROVIDERS.items()}

COOLDOWN_429_SECONDS = int(os.environ.get("KEY_FALLBACK_COOLDOWN_429", "60"))
_FOREVER = 10 ** 9

_lock = threading.Lock()
_use_shared_until = {}  # provider -> monotonic deadline
_installed = False


def _keys(provider):
    """(personal, shared) from the environment, read at call time."""
    _, personal_env, shared_env = PROVIDERS[provider]
    return os.environ.get(personal_env, "").strip(), os.environ.get(shared_env, "").strip()


def _fallback_available(provider):
    personal, shared = _keys(provider)
    return bool(shared) and shared != personal


def _sticky(provider):
    with _lock:
        return time.monotonic() < _use_shared_until.get(provider, 0)


def _decide(provider, status, body_text=""):
    """Seconds to switch to the shared key, or None if the response is fine."""
    if status == 429:
        return COOLDOWN_429_SECONDS
    if status in (401, 402, 403):  # 402 = out of credits (Sarvam: insufficient_quota_error)
        return _FOREVER
    if provider == "gemini" and status == 400:
        low = (body_text or "").lower()
        if "api_key_invalid" in low or "api key not valid" in low or "api key expired" in low:
            return _FOREVER
    return None


def _note(provider, status, seconds):
    with _lock:
        already = time.monotonic() < _use_shared_until.get(provider, 0)
        _use_shared_until[provider] = time.monotonic() + seconds
    if not already:
        when = "for the rest of the run" if seconds >= _FOREVER else f"for {seconds}s"
        print(f"[key_fallback] {provider}: HTTP {status} on personal key -> using shared key {when}")


def switch_to_shared(provider, reason="budget"):
    """Deliberately move a provider to the shared key for the rest of the run.

    Used by the LLM routers when the personal key's per-run call budget is spent
    (that is a self-imposed cap, not a 429, so the normal fallback never fires).
    Returns True if switched, False if there is no distinct shared key.
    """
    if not _fallback_available(provider):
        return False
    with _lock:
        already = time.monotonic() < _use_shared_until.get(provider, 0)
        _use_shared_until[provider] = time.monotonic() + _FOREVER
    if not already:
        print(f"[key_fallback] {provider}: {reason} -> using shared key for the rest of the run")
    return True


def reset():
    """Forget cooldowns (tests / start of a run)."""
    with _lock:
        _use_shared_until.clear()


def _swap_url_key(url, new_key):
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["key"] = new_key
    return urlunsplit(parts._replace(query=urlencode(q)))


def _apply_shared(provider, headers, url):
    """Return (headers, url) carrying the shared key for this provider."""
    _, shared = _keys(provider)
    headers = dict(headers)
    lower = {k.lower(): k for k in headers}
    if provider == "groq":
        headers[lower.get("authorization", "Authorization")] = f"Bearer {shared}"
    elif provider == "pexels":
        headers[lower.get("authorization", "Authorization")] = shared
    elif provider == "sarvam":
        headers[lower.get("api-subscription-key", "api-subscription-key")] = shared
    else:  # gemini: key may travel in a header or in ?key=
        if "x-goog-api-key" in lower:
            headers[lower["x-goog-api-key"]] = shared
        if "key=" in url:
            url = _swap_url_key(url, shared)
        if "x-goog-api-key" not in lower and "key=" not in url:
            headers["x-goog-api-key"] = shared
    return headers, url


# ---------------------------------------------------------------- httpx ----
def _patch_httpx():
    try:
        import httpx
    except ImportError:
        return

    def _clone(request, provider):
        headers, url = _apply_shared(provider, dict(request.headers), str(request.url))
        return httpx.Request(
            request.method, url, headers=headers,
            content=request.read(), extensions=request.extensions,
        )

    orig_send = httpx.Client.send

    def send(self, request, **kwargs):
        provider = HOST_TO_PROVIDER.get(request.url.host)
        if provider is None or not _fallback_available(provider):
            return orig_send(self, request, **kwargs)
        used_shared = False
        if _sticky(provider):
            try:
                request, used_shared = _clone(request, provider), True
            except Exception:
                pass
        resp = orig_send(self, request, **kwargs)
        if used_shared:
            return resp
        body = ""
        if resp.status_code == 400 and not kwargs.get("stream"):
            try:
                resp.read()
                body = resp.text
            except Exception:
                pass
        secs = _decide(provider, resp.status_code, body)
        if secs is None:
            return resp
        try:
            retry = _clone(request, provider)
        except Exception:
            return resp
        _note(provider, resp.status_code, secs)
        resp.close()
        return orig_send(self, retry, **kwargs)

    httpx.Client.send = send

    orig_asend = httpx.AsyncClient.send

    async def asend(self, request, **kwargs):
        provider = HOST_TO_PROVIDER.get(request.url.host)
        if provider is None or not _fallback_available(provider):
            return await orig_asend(self, request, **kwargs)
        used_shared = False
        if _sticky(provider):
            try:
                request, used_shared = _clone(request, provider), True
            except Exception:
                pass
        resp = await orig_asend(self, request, **kwargs)
        if used_shared:
            return resp
        body = ""
        if resp.status_code == 400 and not kwargs.get("stream"):
            try:
                await resp.aread()
                body = resp.text
            except Exception:
                pass
        secs = _decide(provider, resp.status_code, body)
        if secs is None:
            return resp
        try:
            retry = _clone(request, provider)
        except Exception:
            return resp
        _note(provider, resp.status_code, secs)
        await resp.aclose()
        return await orig_asend(self, retry, **kwargs)

    httpx.AsyncClient.send = asend


# ------------------------------------------------------------- requests ----
def _patch_requests():
    try:
        import requests
    except ImportError:
        return

    orig_send = requests.Session.send

    def _clone(prepared, provider):
        new = prepared.copy()
        headers, url = _apply_shared(provider, dict(prepared.headers), prepared.url)
        new.headers.clear()
        new.headers.update(headers)
        new.url = url
        return new

    def send(self, request, **kwargs):
        host = urlsplit(request.url).hostname or ""
        provider = HOST_TO_PROVIDER.get(host)
        if provider is None or not _fallback_available(provider):
            return orig_send(self, request, **kwargs)
        used_shared = False
        if _sticky(provider):
            request, used_shared = _clone(request, provider), True
        resp = orig_send(self, request, **kwargs)
        if used_shared:
            return resp
        body = ""
        if resp.status_code == 400:
            try:
                body = resp.text
            except Exception:
                pass
        secs = _decide(provider, resp.status_code, body)
        if secs is None:
            return resp
        _note(provider, resp.status_code, secs)
        resp.close()
        return orig_send(self, _clone(request, provider), **kwargs)

    requests.Session.send = send


def install():
    """Idempotent. Call once at the top of each scheduler entrypoint."""
    global _installed
    if _installed or os.environ.get("KEY_FALLBACK", "1") == "0":
        return
    _installed = True
    _patch_httpx()
    _patch_requests()
    active = [p for p in PROVIDERS if _fallback_available(p)]
    print(f"[key_fallback] installed; shared-key fallback active for: {', '.join(active) or 'none'}")
