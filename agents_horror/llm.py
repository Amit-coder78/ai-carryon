# agents_horror/llm.py
"""Budgeted LLM access. Reuses the existing Groq-first / Gemini-fallback
safe_invoke from agents_cricket.script_agent (no new provider), but counts calls
so one Horror run can never burn more than HORROR_MAX_CALLS free-tier requests."""
import json
import re


class LLMBudgetExceeded(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    pass


def _default_invoke(prompt):
    from agents_cricket.script_agent import safe_invoke  # existing shared implementation
    return safe_invoke(prompt)


class LLMBudget:
    def __init__(self, max_calls=8, invoke=None):
        self.max_calls = max_calls
        self.calls = 0
        self._invoke = invoke or _default_invoke

    @property
    def remaining(self):
        return self.max_calls - self.calls

    def ask(self, prompt):
        """Returns response text. Raises LLMBudgetExceeded / LLMUnavailable."""
        if self.calls >= self.max_calls:
            raise LLMBudgetExceeded(f"Horror LLM budget of {self.max_calls} calls used up")
        self.calls += 1
        try:
            resp = self._invoke(prompt)
        except Exception as e:
            raise LLMUnavailable(f"LLM call failed: {e}") from e
        text = getattr(resp, "content", resp)
        if isinstance(text, list):  # some providers return content blocks
            text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text)
        text = (text or "").strip()
        if not text:
            raise LLMUnavailable("LLM returned an empty answer")
        return text


def parse_json(text):
    """Pulls the first JSON object/array out of an LLM answer (fences allowed).
    Returns None if nothing parses."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text).strip()
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = t.find(open_c), t.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(t[s:e + 1])
            except ValueError:
                continue
    return None
