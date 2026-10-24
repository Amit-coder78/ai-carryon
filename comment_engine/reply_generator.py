"""Structured reply generation with a single repair retry."""
from pathlib import Path

from pydantic import ValidationError

from .llmjson import LLMOutputError, extract_json, untrusted
from .persona_registry import render_persona
from .schemas import ReplyDraft

PROMPT = (Path(__file__).parent / "prompts" / "generator.txt").read_text(encoding="utf-8")


class GenerationError(Exception):
    pass


def _fmt_list(items, empty="(none)"):
    items = [untrusted(i, 400) for i in items if i]
    return "\n".join(f"- {i}" for i in items) if items else empty


class ReplyGenerator:
    def __init__(self, invoke):
        self.invoke = invoke

    def _prompt(self, comment, analysis, persona, video_context, examples, thread_context,
                retrieved_facts, revision_notes, repair=None):
        ex = "\n".join(f'- Comment: {untrusted(e["comment"], 200)}\n  Reply: {untrusted(e["reply"], 300)}'
                       for e in examples) or "(none)"
        notes = ""
        if revision_notes:
            notes = ("REVISION NOTES (your previous draft failed validation; fix these):\n"
                     + "\n".join(f"- {n}" for n in revision_notes) + "\n\n")
        if repair:
            notes += f"Your previous output was invalid ({repair}). Return only the JSON object.\n\n"
        return (PROMPT
                .replace("{persona_config}", render_persona(persona))
                .replace("{video_context}", untrusted(video_context.as_text(), 2500))
                .replace("{thread_context}", _fmt_list(thread_context))
                .replace("{retrieved_facts}", _fmt_list(list(retrieved_facts) + list(video_context.facts)))
                .replace("{comment_analysis}", analysis.model_dump_json())
                .replace("{examples}", ex)
                .replace("{revision_notes}", notes)
                .replace("{comment_text}", untrusted(comment.text)))

    def generate(self, comment, analysis, persona, video_context, examples=(), thread_context=(),
                 retrieved_facts=(), revision_notes=()):
        error = None
        for _ in range(2):
            try:
                raw = self.invoke(self._prompt(comment, analysis, persona, video_context, examples,
                                               thread_context, retrieved_facts, revision_notes, error))
                data = extract_json(raw)
                data["reply_text"] = str(data.get("reply_text", "")).strip()
                return ReplyDraft.model_validate(data)
            except (LLMOutputError, ValidationError) as e:
                error = str(e)[:160]
        raise GenerationError(f"structured output invalid after retry: {error}")
