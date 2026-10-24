"""Tenant/channel-scoped context: video metadata, thread, FAQs and approved examples."""
import re

from .deduplication import normalize
from .schemas import VideoContext
from .youtube_client import YouTubeAPIError


def _tokens(text):
    return {t for t in normalize(text).split() if len(t) > 2}


class ContextRetriever:
    def __init__(self, repository, youtube=None, script_provider=None):
        self.repo = repository
        self.youtube = youtube                    # YouTubeCommentClient or None
        self.script_provider = script_provider    # optional (channel_id, video_id) -> str | None

    def video_context(self, tenant_id, channel_id, video_id):
        cached = self.repo.get_video(tenant_id, channel_id, video_id)
        title = description = transcript = ""
        if cached:
            title, description, transcript = (cached.get("title") or "", cached.get("description") or "",
                                              cached.get("transcript") or "")
        elif self.youtube is not None and video_id:
            try:
                info = self.youtube.get_video(video_id)
            except YouTubeAPIError:
                info = None                      # context is best-effort; validator still gates claims
            if info:
                title, description = info["title"], info["description"]
        if not transcript and self.script_provider is not None and video_id:
            try:
                transcript = self.script_provider(channel_id, video_id) or ""
            except Exception:
                transcript = ""
        if (title or description or transcript) and not cached:
            self.repo.put_video(tenant_id, channel_id, video_id, title, description, transcript)
        return VideoContext(tenant_id=tenant_id, channel_id=channel_id, video_id=video_id,
                            title=title, description=description, transcript=transcript)

    def faq_facts(self, tenant_id, channel_id, comment_text, limit=3):
        faqs = self.repo.get_settings(tenant_id, channel_id).get("faqs") or []
        want = _tokens(comment_text)
        scored = []
        for f in faqs:
            score = len(want & _tokens(f.get("q", "") + " " + f.get("a", "")))
            if score:
                scored.append((score, f"Q: {f['q']} A: {f['a']}"))
        return [t for _, t in sorted(scored, reverse=True)[:limit]]

    def examples(self, tenant_id, channel_id, persona, comment_text, limit=4):
        pool = [{"comment": e.comment, "reply": e.reply} for e in persona.examples]
        pool += self.repo.approved_examples(tenant_id, channel_id)
        want = _tokens(comment_text)
        ranked = sorted(pool, key=lambda e: len(want & _tokens(e["comment"])), reverse=True)
        return ranked[:limit]
