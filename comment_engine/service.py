"""Comment agent service: sync -> draft -> (owner approval) -> publish.

Safety model
- Drafts never publish themselves. Publishing needs: global switch on, channel not paused,
  channel publishing_enabled by its owner, draft status 'approved' (explicit approval),
  a passing re-validation, a fresh eligibility check, and an atomic approved->publishing claim.
- Timeouts/5xx on insert are AMBIGUOUS: we reconcile against YouTube before any retry.
"""
import time
from datetime import datetime, timedelta, timezone

from .classifier import Classifier
from .config import PIPELINES, STALE_PUBLISHING_MINUTES, Settings, load_settings
from .context_retriever import ContextRetriever
from .deduplication import dedupe_comments
from .orchestrator import Orchestrator
from .persona_registry import PersonaRegistry
from .reply_generator import ReplyGenerator
from .reply_validator import ReplyValidator
from .repository import PROCESSABLE, Repository, now
from .schemas import ReplyDraft
from .youtube_client import ABORT, AMBIGUOUS, PERMANENT, YouTubeAPIError, YouTubeCommentClient

IST = timezone(timedelta(hours=5, minutes=30))


class AgentError(Exception):
    """Base class; `code` is a stable machine-readable reason."""
    code = "error"

    def __init__(self, message="", code=None):
        super().__init__(message or (code or self.code))
        if code:
            self.code = code


class AuthorizationError(AgentError):
    code = "unauthorized"


class PublishBlocked(AgentError):
    code = "publish_blocked"


class CommentAgentService:
    def __init__(self, channel_id, tenant_id=None, settings=None, repository=None, youtube=None,
                 llm=None, personas=None, script_provider=None, log=print, topic_sink="default"):
        self.settings = settings or load_settings()
        self.tenant_id = tenant_id or self.settings.tenant_id
        self.channel_id = channel_id
        self.log = log
        self.authorize()
        self.repo = repository or Repository()
        self.personas = personas or PersonaRegistry(repository=self.repo)
        self._youtube, self._llm, self._script_provider = youtube, llm, script_provider
        self._topic_sink = topic_sink
        self._orch = None

    # -- access control ---------------------------------------------------
    def authorize(self):
        """Single-tenant in this repo: the tenant must match configuration and the channel must be a
        known pipeline. In the SaaS port this becomes a real tenant->channel ownership lookup."""
        if self.channel_id not in PIPELINES:
            raise AuthorizationError(f"unknown channel {self.channel_id!r}")
        if self.tenant_id != self.settings.tenant_id:
            raise AuthorizationError("tenant does not own this channel")

    # -- lazily built collaborators --------------------------------------
    @property
    def youtube(self):
        if self._youtube is None:
            from .channels import build_youtube
            self._youtube = YouTubeCommentClient(build_youtube(self.channel_id))
        return self._youtube

    @property
    def llm(self):
        if self._llm is None:
            from .channels import build_llm
            self._llm = build_llm(self.channel_id)
        return self._llm

    def _orchestrator(self):
        if self._orch is None:
            retr = ContextRetriever(self.repo, youtube=self._youtube_or_none(), script_provider=self._script_provider)
            self._orch = Orchestrator(
                tenant_id=self.tenant_id, channel_id=self.channel_id, settings=self.settings,
                repository=self.repo, personas=self.personas, retriever=retr,
                classifier=Classifier(self.llm),
                generator=ReplyGenerator(self.llm),
                validator=ReplyValidator(self.llm, semantic=self.settings.semantic_check,
                                         duplicate_window=self.settings.duplicate_window,
                                         max_chars=self.settings.max_reply_chars, repository=self.repo),
                model_version=getattr(self.llm, "model_version", None), topic_sink=self._resolve_topic_sink())
        return self._orch

    def _resolve_topic_sink(self):
        if self._topic_sink != "default":
            return self._topic_sink
        from .channels import save_topic_request
        return lambda topic, text, video_id, author: save_topic_request(self.channel_id, topic, text, video_id,
                                                                        author)

    def _youtube_or_none(self):
        try:
            return self.youtube
        except Exception:
            return None

    def _check_active(self, for_publish=False):
        if not self.settings.enabled:
            raise PublishBlocked("comment agent disabled (COMMENT_AGENT_ENABLED)", "disabled")
        st = self.repo.get_settings(self.tenant_id, self.channel_id)
        if st["paused"]:
            raise PublishBlocked("channel is paused", "paused")
        if for_publish and not st["publishing_enabled"]:
            raise PublishBlocked("publishing is not enabled for this channel", "publishing_disabled")
        return st

    # -- sync ------------------------------------------------------------
    def sync(self, max_comments=None):
        self._check_active()
        limit = max_comments or self.settings.max_comments_per_run
        threads = self.youtube.list_threads(max_results=max(limit * 3, 30))
        fresh = [t for t in threads if not t["is_own"] and t["can_reply"] and not t["own_replied"]]
        known = self.repo.known_comment_ids(self.tenant_id, self.channel_id, [t["comment_id"] for t in fresh])
        fresh, repeats = dedupe_comments(fresh, known)
        created = 0
        for item in fresh:
            if self.repo.upsert_comment(self.tenant_id, self.channel_id, item):
                created += 1
        for item in repeats:                      # same author spamming identical text
            if self.repo.upsert_comment(self.tenant_id, self.channel_id, item):
                self.repo.set_comment_status(self.tenant_id, self.channel_id, item["comment_id"], "skipped",
                                             error="repeated_comment_by_author")
        self.repo.update_settings(self.tenant_id, self.channel_id, last_sync_at=now())
        self.repo.record_event(self.tenant_id, self.channel_id, "synced",
                               detail={"seen": len(threads), "new": created})
        return created

    # -- processing -------------------------------------------------------
    def process_pending(self, limit=None):
        self._check_active()
        limit = limit or self.settings.max_comments_per_run
        stats = {"processed": 0, "topic_requests": 0, "pending_approval": 0, "needs_review": 0, "skipped": 0, "failed": 0, "blocked": 0}
        started, consecutive_fail = time.time(), 0
        for row in self.repo.list_comments(self.tenant_id, self.channel_id, PROCESSABLE, limit):
            if time.time() - started > self.settings.run_budget_seconds or consecutive_fail >= 3:
                self.log("[comment-agent] stopping early (budget or repeated failures)")
                break
            outcome = self._orchestrator().process_comment(row["comment_id"])
            stats["topic_requests"] = self._orchestrator().topic_requests_saved
            stats["processed"] += 1
            if outcome in stats:
                stats[outcome] += 1
            consecutive_fail = consecutive_fail + 1 if outcome == "failed" else 0
        return stats

    def process_one(self, comment_id):
        self._check_active()
        return self._orchestrator().process_comment(comment_id)

    def run(self, max_comments=None, publish_approved=True):
        """One scheduled cycle: sync, draft, then publish owner-approved drafts if enabled."""
        stats = {"channel": self.channel_id, "mode": self.settings.default_mode}
        try:
            stats["synced_new"] = self.sync(max_comments)
            stats.update(self.process_pending(max_comments))
        except YouTubeAPIError as e:
            stats["aborted"] = f"{e.kind}:{e.reason}"
            self.repo.record_event(self.tenant_id, self.channel_id, "run_aborted", detail=stats)
            return stats
        if publish_approved and self.settings.default_mode == "publish_approved":
            stats["published"] = self.publish_approved()
        self.log(f"[comment-agent:{self.channel_id}] {stats}")
        return stats

    # -- review actions ----------------------------------------------------
    def list_drafts(self, statuses=("pending_approval", "needs_review", "approved", "publish_failed"), limit=50):
        return self.repo.list_drafts(self.tenant_id, self.channel_id, statuses, limit)

    def _draft(self, draft_id):
        d = self.repo.get_draft(self.tenant_id, self.channel_id, draft_id)
        if d is None:
            raise LookupError("draft not found for this channel")
        return d

    def edit_draft(self, draft_id, text, actor="owner"):
        d = self._draft(draft_id)
        if d["status"] not in ("pending_approval", "needs_review", "edited"):
            raise AgentError(f"cannot edit a draft in status {d['status']}", "bad_state")
        comment = self.repo.get_comment(self.tenant_id, self.channel_id, d["comment_id"])
        persona = self.personas.load(self.tenant_id, self.channel_id)
        retr = ContextRetriever(self.repo)
        video = retr.video_context(self.tenant_id, self.channel_id, comment.video_id)
        v = ReplyValidator(None, semantic=False, duplicate_window=self.settings.duplicate_window,
                           max_chars=self.settings.max_reply_chars, repository=self.repo)
        res = v.validate(self.tenant_id, self.channel_id, comment,
                         ReplyDraft(reply_text=text or "x", strategy="answer"), persona, video,
                         draft_id=draft_id)
        ok = self.repo.transition_draft(self.tenant_id, self.channel_id, draft_id,
                                        ("pending_approval", "needs_review", "edited"), "edited",
                                        reply_text=text, validation_passed=res.passed,
                                        validation_reasons=res.reasons)
        self.repo.record_event(self.tenant_id, self.channel_id, "edited", comment_id=d["comment_id"],
                               draft_id=draft_id, actor=actor, detail={"passed": res.passed, "reasons": res.reasons})
        return {"updated": ok, "validation": res.model_dump()}

    def approve(self, draft_id, actor="owner"):
        d = self._draft(draft_id)
        if not d["validation_passed"]:
            raise AgentError("draft has failing validation; edit it so it passes first", "validation_failed")
        if not (d["reply_text"] or "").strip():
            raise AgentError("draft has no reply text", "empty")
        ok = self.repo.transition_draft(self.tenant_id, self.channel_id, draft_id,
                                        ("pending_approval", "edited"), "approved",
                                        approved_by=actor, approved_at=now())
        if not ok:
            raise AgentError(f"cannot approve a draft in status {d['status']}", "bad_state")
        self.repo.set_comment_status(self.tenant_id, self.channel_id, d["comment_id"], "approved")
        self.repo.record_event(self.tenant_id, self.channel_id, "approved", comment_id=d["comment_id"],
                               draft_id=draft_id, actor=actor)
        return True

    def reject(self, draft_id, actor="owner", reason=""):
        d = self._draft(draft_id)
        ok = self.repo.transition_draft(
            self.tenant_id, self.channel_id, draft_id,
            ("pending_approval", "needs_review", "edited", "approved", "publish_failed"), "rejected",
            review_reason=reason or d.get("review_reason"))
        if ok:
            self.repo.set_comment_status(self.tenant_id, self.channel_id, d["comment_id"], "skipped",
                                         error="rejected_by_owner")
            self.repo.record_event(self.tenant_id, self.channel_id, "rejected", comment_id=d["comment_id"],
                                   draft_id=draft_id, actor=actor, detail={"reason": reason})
        return ok

    def feedback(self, draft_id, label, actor="owner"):
        d = self._draft(draft_id)
        ok = self.repo.set_feedback(self.tenant_id, self.channel_id, draft_id, label)
        self.repo.record_event(self.tenant_id, self.channel_id, "feedback", comment_id=d["comment_id"],
                               draft_id=draft_id, actor=actor, detail={"label": label})
        return ok

    def set_publishing(self, enabled, actor="owner"):
        self.repo.update_settings(self.tenant_id, self.channel_id, publishing_enabled=bool(enabled))
        self.repo.record_event(self.tenant_id, self.channel_id, "publishing_enabled" if enabled else
                               "publishing_disabled", actor=actor)

    def set_paused(self, paused, actor="owner"):
        self.repo.update_settings(self.tenant_id, self.channel_id, paused=bool(paused))
        self.repo.record_event(self.tenant_id, self.channel_id, "paused" if paused else "resumed", actor=actor)

    def set_overrides(self, overrides, actor="owner"):
        from .persona_registry import apply_overrides
        base = self.personas._read(self.personas.persona_id_for(self.channel_id))
        apply_overrides(base, overrides)                      # validates before persisting
        merged = {**self.repo.get_settings(self.tenant_id, self.channel_id)["persona_overrides"], **overrides}
        self.repo.update_settings(self.tenant_id, self.channel_id, persona_overrides=merged)
        self.repo.record_event(self.tenant_id, self.channel_id, "persona_overrides", actor=actor, detail=overrides)

    # -- publishing ---------------------------------------------------------
    def publish(self, draft_id, actor="owner"):
        """Publish one APPROVED draft. Returns the YouTube reply id."""
        t, c = self.tenant_id, self.channel_id
        self._check_active(for_publish=True)
        d = self._draft(draft_id)
        if d["status"] == "published" or d.get("published_reply_id"):
            raise PublishBlocked("already published", "already_published")
        if d["status"] == "publishing":
            self.repo.reclaim_stale_publishing(t, c, draft_id, STALE_PUBLISHING_MINUTES)
            d = self._draft(draft_id)
        if d["status"] not in ("approved", "publish_failed"):
            raise PublishBlocked(f"draft is {d['status']}, not approved", "not_approved")
        if d["status"] == "publish_failed" and not d.get("approved_by"):
            raise PublishBlocked("draft was never approved", "not_approved")
        comment = self.repo.get_comment(t, c, d["comment_id"])
        persona = self.personas.load(t, c)
        if persona.publishing.mode == "draft_only":
            raise PublishBlocked("persona is draft_only", "draft_only")

        # re-validate right before publishing (deterministic only)
        retr = ContextRetriever(self.repo)
        video = retr.video_context(t, c, comment.video_id)
        v = ReplyValidator(None, semantic=False, duplicate_window=self.settings.duplicate_window,
                           max_chars=self.settings.max_reply_chars, repository=self.repo)
        res = v.validate(t, c, comment, ReplyDraft(reply_text=d["reply_text"], strategy="answer"),
                         persona, video, draft_id=draft_id)
        if not res.passed:
            self.repo.transition_draft(t, c, draft_id, ("approved", "publish_failed"), "needs_review",
                                       validation_passed=False, validation_reasons=res.reasons)
            raise PublishBlocked(f"re-validation failed: {res.reasons}", "revalidation_failed")

        # fresh eligibility + reconciliation (covers prior ambiguous timeouts and manual replies)
        try:
            thread = self.youtube.get_thread(comment.comment_id)
            if thread is None or not thread["can_reply"]:
                self._publish_failed(d, "comment_unavailable", PERMANENT)
                raise PublishBlocked("comment is no longer replyable", "comment_unavailable")
            existing = self.youtube.find_reply(comment.comment_id, d["reply_text"])
            if existing:
                return self._mark_published(d, existing, actor, reconciled=True)
            if thread["own_replied"]:
                self._publish_failed(d, "already_replied", PERMANENT)
                raise PublishBlocked("channel already replied in this thread", "already_replied")
        except YouTubeAPIError as e:
            self._publish_failed(d, f"{e.kind}:{e.reason}", e.kind)
            raise PublishBlocked(f"pre-publish check failed: {e}", e.reason)

        if not self.repo.transition_draft(t, c, draft_id, ("approved", "publish_failed"), "publishing"):
            raise PublishBlocked("another worker is publishing this draft", "claimed")
        try:
            reply_id = self.youtube.insert_reply(comment.comment_id, d["reply_text"])
        except YouTubeAPIError as e:
            if e.kind == AMBIGUOUS:                           # may have posted; reconcile now
                try:
                    found = self.youtube.find_reply(comment.comment_id, d["reply_text"])
                except YouTubeAPIError:
                    found = None
                if found:
                    return self._mark_published(d, found, actor, reconciled=True)
            self._publish_failed(d, f"{e.kind}:{e.reason}", e.kind, from_status="publishing")
            raise PublishBlocked(f"publish failed: {e}", e.reason)
        return self._mark_published(d, reply_id, actor, from_status="publishing")

    def _mark_published(self, d, reply_id, actor, reconciled=False, from_status=None):
        t, c = self.tenant_id, self.channel_id
        self.repo.transition_draft(t, c, d["draft_id"], (from_status,) if from_status else
                                   ("approved", "publish_failed", "publishing"), "published",
                                   published_reply_id=reply_id, last_error=None)
        self.repo.set_comment_status(t, c, d["comment_id"], "published")
        self.repo.record_event(t, c, "published", comment_id=d["comment_id"], draft_id=d["draft_id"], actor=actor,
                               detail={"reply_id": reply_id, "reconciled": reconciled})
        return reply_id

    def _publish_failed(self, d, error, kind, from_status=None):
        t, c = self.tenant_id, self.channel_id
        froms = (from_status,) if from_status else ("approved", "publish_failed")
        self.repo.transition_draft(t, c, d["draft_id"], froms, "publish_failed", last_error=error)
        if kind == PERMANENT:
            self.repo.set_comment_status(t, c, d["comment_id"], "blocked", error=error)
        self.repo.record_event(t, c, "publish_failed", comment_id=d["comment_id"], draft_id=d["draft_id"],
                               detail={"error": error, "kind": kind})

    def publish_approved(self):
        """Publish drafts the owner approved, if the channel owner enabled publishing."""
        try:
            self._check_active(for_publish=True)
        except PublishBlocked as e:
            self.log(f"[comment-agent:{self.channel_id}] not publishing: {e.code}")
            return 0
        persona = self.personas.load(self.tenant_id, self.channel_id)
        cap = persona.publishing.max_replies_per_run
        done = 0
        for d in reversed(self.repo.list_drafts(self.tenant_id, self.channel_id, ("approved",), cap)):
            try:
                self.publish(d["draft_id"], actor="scheduler")
                done += 1
                time.sleep(self.settings.publish_delay_seconds)
            except PublishBlocked as e:
                if e.code in ("disabled", "paused", "publishing_disabled"):
                    break
                self.log(f"[comment-agent:{self.channel_id}] {d['draft_id']}: {e.code}")
            except YouTubeAPIError as e:
                if e.kind == ABORT:
                    break
        return done

    # -- scheduling -----------------------------------------------------------
    def run_scheduled(self, force=False):
        """Daily gate for the generic schedulers (IST window, once per IST day). Returns stats or None."""
        if not force:
            if self.channel_id not in self.settings.channels:
                return None
            ist = datetime.now(IST)
            if not (self.settings.window_start_hour_ist <= ist.hour <= self.settings.window_end_hour_ist):
                return None
            key = f"comment_agent_last_run:{self.tenant_id}:{self.channel_id}"
            if self.repo.db.get_meta(key) == ist.strftime("%Y-%m-%d"):
                return None
            self.repo.db.set_meta(key, ist.strftime("%Y-%m-%d"))
        return self.run()


def run_scheduled(channel_id, force=False, log=print):
    """Entry point for scheduler hooks. Never raises (a comment-agent failure must not break a pipeline)."""
    try:
        settings = load_settings()
        if not settings.enabled:
            return None
        import os
        force = force or os.getenv("COMMENT_FORCE", "").lower() == "true"
        return CommentAgentService(channel_id, settings=settings, log=log).run_scheduled(force=force)
    except Exception as e:
        log(f"[comment-agent:{channel_id}] error: {type(e).__name__}: {e}")
        return None
