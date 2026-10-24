"""Draft validation: deterministic hard constraints first, optional LLM semantic check after.

Reason codes are stable strings; the orchestrator regenerates once for fixable
reasons and routes everything else to a human.
"""
import re
from pathlib import Path

from pydantic import ValidationError  # noqa: F401  (re-exported for callers)

from .deduplication import is_duplicate_reply, normalize
from .llmjson import LLMOutputError, extract_json, untrusted
from .persona_registry import render_persona
from .schemas import ReplyValidation

PROMPT = (Path(__file__).parent / "prompts" / "validator.txt").read_text(encoding="utf-8")

HARD_BLOCK = {"id_mismatch", "already_published"}   # never regenerated, never published

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|net|org|io|co|ly|me|xyz)\b)", re.I)
_DEBUG = re.compile(r"(as an ai( language model)?|system prompt|persona|json|```|<[a-z/][^>]*>|\{.*\}|"
                    r"reply\s*:|draft\s*:|classification|i cannot comply|\[.*?\])", re.I)
_GENERIC = re.compile(r"^(thank you for (your )?(comment|feedback|watching)|thanks for (your )?(comment|feedback|watching)|"
                      r"great question|i understand( your)?|dear (viewer|user|friend)|we appreciate|"
                      r"hello dear|as a (creator|channel)|valued (viewer|subscriber))", re.I)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F000-\U0001F2FF]")
_NUMBER = re.compile(r"\d[\d,.]*%?")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_SENTENCE_END = re.compile(r"[.!?\u0964]+(\s|$)")

_GUARANTEE = re.compile(r"(guarantee[ds]?|sure[- ]?shot|risk[- ]?free|no risk|will (definitely|surely) (go up|double|rise)|"
                        r"100% (safe|return|profit)|assured returns?|can't lose|cannot lose|double your)", re.I)
_PERSONAL_ADVICE = re.compile(r"\b(you should (buy|sell|invest|put)|buy (it )?now|sell (it )?now|go all in|"
                              r"put (all|everything)|invest in)\b", re.I)
_MIRACLE = re.compile(r"(will (definitely|surely|certainly) (be )?(fulfil|come true|cure|heal|get)|wish will (be )?(fulfil|come true)|"
                      r"manokamna poori|icchha poori|guaranteed (blessing|result)|chamatkar|miracle)", re.I)
_SCRIPTURE = re.compile(r"(\b(gita|ramayan|mahabharat|bhagavad|veda|upanishad|purana)\b[^.!?]{0,40}\b(says|mein likha|states|verse|shlok|chapter)\b|"
                        r"\bshlok(a)?\b|\bverse \d|\b\d+[.:]\d+\b)", re.I)
_QUOTE = re.compile(r"[\"“«‘']([^\"”»’']{25,})[\"”»’']")


def _sentences(text):
    parts = [p for p in _SENTENCE_END.split(text) if p and p.strip() and not re.fullmatch(r"[.!?\u0964]+", p.strip())]
    return max(1, len([p for p in parts if re.search(r"\w", p)]))


def _emoji_count(text):
    return len(_EMOJI.findall(text))


def _numbers(text):
    return {n.rstrip(".,").replace(",", "") for n in _NUMBER.findall(text)}


class ReplyValidator:
    def __init__(self, invoke=None, semantic=True, duplicate_window=100, max_chars=500, repository=None):
        self.invoke = invoke
        self.semantic = semantic and invoke is not None
        self.duplicate_window = duplicate_window
        self.max_chars = max_chars
        self.repository = repository

    # -- deterministic -------------------------------------------------------
    def check_deterministic(self, tenant_id, channel_id, comment, draft_text, persona, video_context,
                            extra_context="", recent_replies=None, draft_id=None):
        reasons, review = [], False
        text = (draft_text or "").strip()

        if not text:
            return ["empty_reply"], True
        if comment.tenant_id != tenant_id or comment.channel_id != channel_id or (
                video_context is not None and (video_context.tenant_id != tenant_id
                                               or video_context.channel_id != channel_id
                                               or video_context.video_id != comment.video_id)):
            return ["id_mismatch"], True
        if len(text) > self.max_chars:
            reasons.append("too_long")
        if _CTRL.search(text):
            reasons.append("control_characters")
        if _DEBUG.search(text):
            reasons.append("prompt_or_debug_text")
        if _URL.search(text) or "@" in text:
            reasons.append("link_or_mention")
        if _sentences(text) > persona.voice.max_sentences:
            reasons.append("too_many_sentences")
        if persona.voice.avoid_generic_openings and _GENERIC.search(text):
            reasons.append("generic_opening")
        emojis = _emoji_count(text)
        level = persona.voice.emoji_level
        if (level == "none" and emojis) or (level == "low" and emojis > 1) or (level == "moderate" and emojis > 3):
            reasons.append("emoji_level")
        if persona.language == "english" and len(_DEVANAGARI.findall(text)) > 0.3 * max(1, len(text)):
            reasons.append("language_mismatch")

        recent = recent_replies
        if recent is None and self.repository is not None:
            recent = self.repository.recent_reply_texts(tenant_id, channel_id, self.duplicate_window,
                                                        exclude_draft_id=draft_id)
        if persona.publishing.prevent_duplicate_replies and is_duplicate_reply(text, recent or []):
            reasons.append("duplicate_reply")

        d = persona.domain
        if d.no_guaranteed_returns and _GUARANTEE.search(text):
            reasons.append("guaranteed_returns")
        if d.no_personalized_financial_advice and _PERSONAL_ADVICE.search(text):
            reasons.append("personalized_financial_advice")
            review = True
        if d.no_miracle_or_divine_outcome_claims and _MIRACLE.search(text):
            reasons.append("divine_outcome_claim")
        if d.no_fabricated_scripture:
            ground = normalize(extra_context + " " + comment.text)
            quotes = [q for q in _QUOTE.findall(text) if normalize(q) not in ground]
            if quotes or _SCRIPTURE.search(text) and not _SCRIPTURE.search(extra_context + " " + comment.text):
                reasons.append("unsupported_scripture_or_quote")
                review = True
        if d.require_grounding_for_statistics or d.do_not_invent_scores_or_quotes:
            allowed = _numbers(comment.text + " " + extra_context)
            unsupported = [n for n in _numbers(text) if n not in allowed]
            if unsupported and (d.require_grounding_for_statistics or len(unsupported) > 0):
                reasons.append("unsupported_statistic")
        return reasons, review

    # -- full ---------------------------------------------------------------
    def validate(self, tenant_id, channel_id, comment, draft, persona, video_context,
                 retrieved_facts=(), thread_context=(), recent_replies=None, draft_id=None, semantic=None):
        extra = " ".join([video_context.as_text(6000)] + list(video_context.facts) + list(retrieved_facts)
                         + list(thread_context)) if video_context else " ".join(list(retrieved_facts))
        reasons, review = self.check_deterministic(
            tenant_id, channel_id, comment, draft.reply_text, persona, video_context, extra,
            recent_replies, draft_id)
        if draft.needs_review:
            reasons.append(f"model_flagged_review:{draft.review_reason or 'unspecified'}")
            review = True
        run_semantic = self.semantic if semantic is None else (semantic and self.invoke is not None)
        if not reasons and run_semantic:
            sem_reasons, sem_review = self._semantic(comment, draft, persona, video_context, extra)
            reasons += sem_reasons
            review = review or sem_review
        return ReplyValidation(passed=not reasons, reasons=reasons, requires_human_review=review)

    def _semantic(self, comment, draft, persona, video_context, extra):
        prompt = (PROMPT.replace("{persona_summary}", render_persona(persona))
                  .replace("{video_context}", untrusted(extra, 3500))
                  .replace("{comment_text}", untrusted(comment.text))
                  .replace("{reply_text}", untrusted(draft.reply_text)))
        try:
            data = extract_json(self.invoke(prompt))
        except (LLMOutputError, Exception):
            return ["semantic_check_unavailable"], True      # fail closed
        keys = {"addresses_comment": "does_not_address_comment",
                "relevant_to_video": "not_relevant_to_video",
                "claims_supported_by_context": "unsupported_claims",
                "tone_matches_persona": "tone_mismatch",
                "not_generic": "too_generic",
                "no_private_info_or_rule_violation": "rule_violation"}
        reasons = [code for k, code in keys.items() if data.get(k) is not True]
        return reasons, any(r in ("unsupported_claims", "rule_violation") for r in reasons)
