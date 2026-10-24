"""Per-comment pipeline (spec section 15). Each method maps 1:1 to a graph node so the
flow can move to LangGraph in the SaaS repo without changing behaviour.

  claim -> load_persona -> load_context -> classify -> route
        -> retrieve -> generate -> validate (<=1 regeneration) -> save_draft
"""
import time

from .config import MAX_ATTEMPTS, PROMPT_VERSIONS
from .persona_registry import PersonaError
from .reply_generator import GenerationError
from .reply_validator import HARD_BLOCK


class Orchestrator:
    def __init__(self, *, tenant_id, channel_id, settings, repository, personas, retriever,
                 classifier, generator, validator, model_version=None, topic_sink=None):
        self.tenant_id, self.channel_id, self.settings = tenant_id, channel_id, settings
        self.repo, self.personas, self.retriever = repository, personas, retriever
        self.classifier, self.generator, self.validator = classifier, generator, validator
        self.model_version = model_version
        self.topic_sink = topic_sink          # callable(topic, comment_text, video_id, author_channel_id)
        self.topic_requests_saved = 0

    # -- helpers --------------------------------------------------------
    def _event(self, etype, comment_id, **detail):
        self.repo.record_event(self.tenant_id, self.channel_id, etype, comment_id=comment_id,
                               detail=detail or None)

    def _status(self, comment_id, status, **kw):
        self.repo.set_comment_status(self.tenant_id, self.channel_id, comment_id, status, **kw)

    def _save_review(self, comment_id, persona, text, strategy, reasons, review_reason, passed=False):
        draft_id = self.repo.create_draft(
            self.tenant_id, self.channel_id, comment_id, reply_text=text, strategy=strategy,
            status="needs_review", persona_id=persona.persona_id, persona_version=persona.version,
            prompt_version=PROMPT_VERSIONS["generator"], validation_passed=passed,
            validation_reasons=reasons, review_reason=review_reason, model_version=self.model_version)
        self._status(comment_id, "needs_review")
        self._event("needs_review", comment_id, draft_id=draft_id, reason=review_reason, reasons=reasons)
        return "needs_review"

    def _record_topic_request(self, comment, analysis):
        """Viewer video requests feed the topic_requests table (Brain demand signal). Saved once per
        comment; a sink failure never affects reply handling."""
        if self.topic_sink is None or analysis.intent != "suggestion" or not analysis.requested_topic:
            return
        t, c, cid = self.tenant_id, self.channel_id, comment.comment_id
        try:
            if self.repo.event_exists(t, c, cid, "topic_request_saved"):
                return
            self.topic_sink(analysis.requested_topic, comment.text, comment.video_id, comment.author_channel_id or None)
            self.repo.record_event(t, c, "topic_request_saved", comment_id=cid,
                                   detail={"topic": analysis.requested_topic})
            self.topic_requests_saved += 1
        except Exception as e:
            self._event("topic_request_failed", cid, error=str(e)[:200])

    # -- the pipeline ----------------------------------------------------
    def process_comment(self, comment_id):
        t, c = self.tenant_id, self.channel_id
        comment = self.repo.get_comment(t, c, comment_id)
        if comment is None:
            raise LookupError(f"comment {comment_id} not found for this channel")
        if not self.repo.claim_for_processing(t, c, comment_id):
            return "already_processing_or_done"
        started = time.time()
        try:
            persona = self.personas.load(t, c)                       # fail closed on bad persona
        except PersonaError as e:
            self._status(comment_id, "failed", error=f"persona_config: {e}"[:300], bump_attempts=True)
            self._event("failed", comment_id, error="persona_config")
            return "failed"
        try:
            return self._run(comment, persona, started)
        except Exception as e:                                       # transient (LLM/network): retry later
            attempts = comment.attempts + 1
            if attempts >= MAX_ATTEMPTS:
                self._status(comment_id, "blocked", error=f"dead_letter: {type(e).__name__}: {e}"[:300],
                             bump_attempts=True)
                self._event("dead_letter", comment_id, error=str(e)[:200])
                return "blocked"
            self._status(comment_id, "failed", error=f"{type(e).__name__}: {e}"[:300], bump_attempts=True)
            self._event("failed", comment_id, error=str(e)[:200], attempts=attempts)
            return "failed"

    def _run(self, comment, persona, started):
        t, c, cid = self.tenant_id, self.channel_id, comment.comment_id
        video = self.retriever.video_context(t, c, comment.video_id)
        analysis = self.classifier.classify(comment, video, persona)
        self._status(cid, "processing", intent=analysis.intent)
        self._event("classified", cid, **analysis.model_dump())
        self._record_topic_request(comment, analysis)

        if analysis.reply_strategy == "skip":
            self._status(cid, "skipped", error=",".join(analysis.risk_flags) or analysis.intent)
            self._event("skipped", cid, flags=analysis.risk_flags)
            return "skipped"
        if analysis.reply_strategy == "human_review":
            return self._save_review(cid, persona, "", "human_review", analysis.risk_flags,
                                     "classifier_requested_review:" + ",".join(analysis.risk_flags or ["unspecified"]))

        examples = self.retriever.examples(t, c, persona, comment.text)
        facts = self.retriever.faq_facts(t, c, comment.text)
        thread = []
        notes = []
        draft = validation = None
        for attempt in range(self.settings.max_regeneration_attempts + 1):
            try:
                draft = self.generator.generate(comment, analysis, persona, video, examples, thread, facts, notes)
            except GenerationError as e:
                return self._save_review(cid, persona, "", analysis.reply_strategy, ["generation_failed"], str(e))
            if draft.strategy == "skip":
                self._status(cid, "skipped", error="model_chose_skip")
                self._event("skipped", cid, flags=["model_chose_skip"])
                return "skipped"
            validation = self.validator.validate(t, c, comment, draft, persona, video, facts, thread)
            self._event("validated", cid, attempt=attempt, **validation.model_dump())
            if validation.passed or validation.requires_human_review or set(validation.reasons) & HARD_BLOCK:
                break
            notes = [f"Fix: {r}" for r in validation.reasons]       # one regeneration with the reasons

        latency = int((time.time() - started) * 1000)
        if set(validation.reasons) & HARD_BLOCK:
            self._status(cid, "blocked", error=",".join(validation.reasons))
            self._event("blocked", cid, reasons=validation.reasons)
            return "blocked"
        if not validation.passed:
            return self._save_review(cid, persona, draft.reply_text, draft.strategy, validation.reasons,
                                     draft.review_reason or "validation_failed")
        draft_id = self.repo.create_draft(
            t, c, cid, reply_text=draft.reply_text, strategy=draft.strategy, status="pending_approval",
            persona_id=persona.persona_id, persona_version=persona.version,
            prompt_version=PROMPT_VERSIONS["generator"], validation_passed=True, validation_reasons=[],
            model_version=self.model_version, latency_ms=latency)
        self._status(cid, "pending_approval")
        self._event("drafted", cid, draft_id=draft_id, latency_ms=latency)
        return "pending_approval"
