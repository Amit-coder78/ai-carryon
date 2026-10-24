"""Pydantic data contracts for every stage (spec section 6) plus persona config."""
from typing import Any, Literal, Optional

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

CommentIntent = Literal[
    "question", "praise", "criticism", "joke", "debate",
    "suggestion", "spam", "sensitive", "other",
]
ReplyStrategy = Literal[
    "answer", "banter", "acknowledge", "challenge",
    "ask_clarification", "skip", "human_review",
]
FEEDBACK_LABELS = ("on_brand", "too_generic", "too_polite", "too_aggressive", "incorrect", "irrelevant")


class CommentAnalysis(BaseModel):
    intent: CommentIntent
    language: str
    needs_video_context: bool = True
    reply_strategy: ReplyStrategy
    risk_flags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    requested_topic: str = ""     # only for intent "suggestion": feeds topic_requests (Brain demand signal)

    @field_validator("requested_topic", mode="before")
    @classmethod
    def _clean_topic(cls, v):
        v = re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).strip().strip("\"'")
        return re.sub(r"\s+", " ", v)[:120]


class ReplyDraft(BaseModel):
    reply_text: str = Field(min_length=1, max_length=1000)
    strategy: ReplyStrategy
    needs_review: bool = False
    review_reason: Optional[str] = None
    used_context_ids: list[str] = Field(default_factory=list)


class ReplyValidation(BaseModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    requires_human_review: bool = False


# ---- persona -------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")   # typos in YAML fail closed


class Voice(_Strict):
    identity: str
    traits: list[str] = Field(default_factory=list)
    humor_level: int = Field(3, ge=0, le=5)
    assertiveness: int = Field(3, ge=0, le=5)
    emoji_level: Literal["none", "low", "moderate", "high"] = "low"
    slang_level: Literal["none", "low", "moderate", "high"] = "low"
    max_sentences: int = Field(2, ge=1, le=6)
    avoid_generic_openings: bool = True
    allow_playful_debate: bool = False


class DomainRules(_Strict):
    use_video_context: bool = True
    require_grounding_for_statistics: bool = False
    verify_current_match_facts: bool = False
    do_not_invent_scores_or_quotes: bool = False
    no_guaranteed_returns: bool = False
    no_personalized_financial_advice: bool = False
    require_trusted_sources_for_figures: bool = False
    no_fabricated_scripture: bool = False
    no_miracle_or_divine_outcome_claims: bool = False
    skip_religious_debate: bool = False
    no_monetizing_distress: bool = False
    review_keywords: list[str] = Field(default_factory=list)  # send to human review
    topic_request_instruction: str = ("A short video topic (5-10 words, in English) the viewer is asking "
                                      "for, even if the comment is Hindi/Hinglish.")


class SafetyRules(_Strict):
    avoid_personal_attacks: bool = True
    no_threats_or_harassment: bool = True
    skip_spam: bool = True
    escalate_sensitive_cases: bool = True


class PublishingRules(_Strict):
    mode: Literal["draft_only", "approval_required", "auto"] = "approval_required"
    max_replies_per_run: int = Field(20, ge=1)
    prevent_duplicate_replies: bool = True


class Example(_Strict):
    comment: str
    reply: str


class PersonaConfig(_Strict):
    schema_version: int
    persona_id: str
    display_name: str
    language: Literal["english", "hindi", "hinglish"]
    voice: Voice
    domain: DomainRules = Field(default_factory=DomainRules)
    safety: SafetyRules = Field(default_factory=SafetyRules)
    publishing: PublishingRules = Field(default_factory=PublishingRules)
    extra_rules: list[str] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)
    version: str = "1"        # filled by the registry: schema_version[+overrides revision]


# ---- runtime records ------------------------------------------------------

class Comment(BaseModel):
    tenant_id: str
    channel_id: str
    comment_id: str
    video_id: str = ""
    text: str
    author_name: str = ""
    author_channel_id: str = ""
    parent_comment_id: Optional[str] = None
    published_at: str = ""
    status: str = "fetched"
    attempts: int = 0
    intent: Optional[str] = None


class VideoContext(BaseModel):
    tenant_id: str
    channel_id: str
    video_id: str
    title: str = ""
    description: str = ""
    transcript: str = ""
    facts: list[str] = Field(default_factory=list)

    def as_text(self, limit: int = 3000) -> str:
        parts = [f"Title: {self.title}"] if self.title else []
        if self.description:
            parts.append(f"Description: {self.description[:800]}")
        if self.transcript:
            parts.append(f"Script/transcript: {self.transcript[:2000]}")
        return "\n".join(parts)[:limit] or "(no video context available)"


JSON = dict[str, Any]
