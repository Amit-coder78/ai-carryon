"""Shared helpers: extract a JSON object from model output; neutralise delimiters."""
import json
import re

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class LLMOutputError(ValueError):
    pass


def extract_json(raw):
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMOutputError(f"no JSON object in model output: {text[:120]!r}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMOutputError(f"invalid JSON from model: {e}") from e
    if not isinstance(data, dict):
        raise LLMOutputError("model output is not a JSON object")
    return data


def untrusted(text, limit=1000):
    """Make viewer/retrieved text safe to embed between our delimiters."""
    t = _CTRL.sub("", str(text or ""))
    for marker in ("<<<", ">>>", "COMMENT>>>", "DRAFT>>>"):
        t = t.replace(marker, "")
    return t.strip()[:limit]


def text_of(resp):
    """Normalise langchain messages / plain strings from the channel LLM wrappers."""
    return getattr(resp, "content", resp) if not isinstance(resp, str) else resp
