"""Minimal LLM client for Finance: Groq primary, Gemini fallback (models per README). stdlib only.
Swap `invoke` for the shared routing layer once its signature is confirmed; Finance code only needs prompt->str."""
import json, os, urllib.request

GROQ_MODEL = os.getenv("FINANCE_GROQ_MODEL", "openai/gpt-oss-120b")
GEMINI_MODEL = os.getenv("FINANCE_GEMINI_MODEL", "gemini-3.5-flash")


class LLMError(RuntimeError):
    pass


def _post(url, payload, headers, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "User-Agent": "ai-carryon-finance/1.0", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def invoke(prompt, system="", temperature=0.4, max_tokens=1500, post=_post, env=None):
    e = os.environ if env is None else env
    errors = []
    if e.get("GROQ_API_KEY"):
        try:
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            r = post("https://api.groq.com/openai/v1/chat/completions",
                     {"model": GROQ_MODEL, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
                     {"Authorization": f"Bearer {e['GROQ_API_KEY']}"})
            return r["choices"][0]["message"]["content"]
        except Exception as ex:
            errors.append(f"groq: {ex}")
    if e.get("GEMINI_API_KEY"):
        try:
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
            if system: body["systemInstruction"] = {"parts": [{"text": system}]}
            r = post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={e['GEMINI_API_KEY']}", body, {})
            return r["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as ex:
            errors.append(f"gemini: {ex}")
    raise LLMError("no LLM available: " + ("; ".join(errors) or "no API keys set"))
