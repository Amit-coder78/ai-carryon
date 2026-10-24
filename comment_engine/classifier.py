"""Comment classification: deterministic screen first, then one LLM call.

Comments, descriptions and transcripts are untrusted data. Anything that looks
like spam, a threat, personal data or a prompt-injection attempt is routed
without letting the text influence the system prompt.
"""
import re
from pathlib import Path

from pydantic import ValidationError

from .config import CLASSIFIER_MIN_CONFIDENCE
from .llmjson import LLMOutputError, extract_json, untrusted
from .persona_registry import render_persona
from .schemas import CommentAnalysis

PROMPT = (Path(__file__).parent / "prompts" / "classifier.txt").read_text(encoding="utf-8")

_URL = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|net|org|io|co|ly|me|xyz|shop|top)\b)", re.I)
_PROMO = re.compile(
    r"(subscribe to my|sub4sub|check (out )?my (channel|profile)|whats?app|telegram|t\.me/|"
    r"dm me|free (gift|robux|v-?bucks|crypto)|double your|earn \$?\d+|giveaway winner|"
    r"click (the )?link|promo ?code)", re.I)
_THREAT = re.compile(
    r"(i('| a)?ll|i will|gonna) (kill|find|hurt|beat|doxx?)|know where you live|"
    r"\b(kill yourself|kys)\b|\bswat(ting)?\b", re.I)
_PERSONAL_DATA = re.compile(r"(\b\d{10}\b|\b[\w.+-]+@[\w-]+\.[\w.]+\b|\b\d{4}[ -]\d{4}[ -]\d{4}\b)")
_INJECTION = re.compile(
    r"(ignore (all |the )?(previous|above|prior) (instructions|prompt)|system prompt|"
    r"you are now|disregard (your|the) (rules|instructions)|reveal your (prompt|instructions)|"
    r"act as (an? )?(?!fan)\w+ and|developer mode)", re.I)


def _analysis(intent, strategy, flags, language="unknown", confidence=1.0):
    return CommentAnalysis(intent=intent, language=language, needs_video_context=False,
                           reply_strategy=strategy, risk_flags=flags, confidence=confidence)


def pre_screen(text, persona):
    """Deterministic routing. Returns CommentAnalysis or None to continue to the LLM."""
    t = (text or "").strip()
    if not t or not re.search(r"\w", t, re.UNICODE) and len(t) < 2:
        return _analysis("other", "skip", ["empty"])
    if _INJECTION.search(t):
        return _analysis("spam", "skip", ["prompt_injection"])
    if persona.safety.no_threats_or_harassment and _THREAT.search(t):
        return _analysis("sensitive", "human_review", ["threat_or_harassment"])
    if _PERSONAL_DATA.search(t):
        return _analysis("sensitive", "human_review", ["personal_data"])
    if persona.safety.skip_spam and (_PROMO.search(t) or _URL.search(t)):
        return _analysis("spam", "skip", ["spam_or_link"])
    lowered = t.lower()
    for kw in persona.domain.review_keywords:
        if kw.lower() in lowered:
            return _analysis("sensitive", "human_review", [f"domain_review:{kw}"], confidence=0.9)
    return None


class Classifier:
    def __init__(self, invoke, min_confidence=CLASSIFIER_MIN_CONFIDENCE):
        self.invoke = invoke
        self.min_confidence = min_confidence

    def _prompt(self, comment, video_context, persona, repair=None):
        p = (PROMPT.replace("{persona_summary}", render_persona(persona))
             .replace("{topic_instruction}", persona.domain.topic_request_instruction)
             .replace("{video_context}", untrusted(video_context.as_text() if video_context else "", 1500))
             .replace("{comment_text}", untrusted(comment.text)))
        if repair:
            p += f"\n\nYour previous output was invalid ({repair}). Return only the JSON object."
        return p

    def classify(self, comment, video_context, persona):
        pre = pre_screen(comment.text, persona)
        if pre is not None:
            return pre
        error = None
        for attempt in range(2):   # one repair retry, then route to review
            try:
                raw = self.invoke(self._prompt(comment, video_context, persona, error))
                analysis = CommentAnalysis.model_validate(extract_json(raw))
                return self._apply_rules(analysis, persona)
            except (LLMOutputError, ValidationError) as e:
                error = str(e)[:160]
        return _analysis("other", "human_review", ["classifier_parse_error"], confidence=0.0)

    def _apply_rules(self, a, persona):
        if a.intent == "spam" and persona.safety.skip_spam:
            a.reply_strategy = "skip"
        elif a.intent == "sensitive" and persona.safety.escalate_sensitive_cases and a.reply_strategy != "skip":
            a.reply_strategy = "human_review"
        elif persona.domain.skip_religious_debate and a.intent == "debate":
            a.reply_strategy = "skip"
            a.risk_flags.append("religious_debate")
        elif a.confidence < self.min_confidence and a.reply_strategy not in ("skip", "human_review"):
            a.reply_strategy = "human_review"
            a.risk_flags.append("low_confidence")
        return a
