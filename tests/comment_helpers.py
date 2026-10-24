"""Shared fakes for comment-engine tests (no network, no real LLM)."""
import json
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "t.db"))            # must be set before agents.database loads
os.environ.setdefault("CRICKET_DB_PATH", os.path.join(_tmp, "c.db"))

from agents.database import Database  # noqa: E402
from comment_engine.config import Settings  # noqa: E402
from comment_engine.repository import Repository  # noqa: E402
from comment_engine.service import CommentAgentService  # noqa: E402


class FakeLLM:
    """Routes by prompt type. Override `handlers` per test."""
    def __init__(self, classify=None, generate=None, judge=None):
        self.classify = classify or {"intent": "question", "language": "english", "needs_video_context": True,
                                     "reply_strategy": "answer", "risk_flags": [], "confidence": 0.9}
        self.generate = generate if generate is not None else [
            {"reply_text": "Good question, the answer is in the middle of the video.", "strategy": "answer",
             "needs_review": False, "review_reason": None, "used_context_ids": []}]
        self.judge = judge or {"addresses_comment": True, "relevant_to_video": True,
                               "claims_supported_by_context": True, "tone_matches_persona": True,
                               "not_generic": True, "no_private_info_or_rule_violation": True, "issues": []}
        self.calls, self.prompts = [], []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if "You classify one YouTube comment" in prompt:
            kind, out = "classify", self.classify
        elif "You review a drafted YouTube reply" in prompt:
            kind, out = "judge", self.judge
        else:
            kind = "generate"
            seq = self.generate
            idx = sum(1 for k in self.calls if k == "generate")
            out = seq[min(idx, len(seq) - 1)]
        self.calls.append(kind)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out)


class FakeYouTube:
    def __init__(self, threads=None, own="UC_OWN"):
        self.threads = threads or []
        self.own = own
        self.inserted, self.insert_error, self.posted_text_visible = [], None, {}
        self.find_calls = 0

    def own_channel_id(self):
        return self.own

    def list_threads(self, max_results=50, max_pages=3):
        return list(self.threads)

    def get_video(self, video_id):
        return {"title": "Why Bumrah's yorker works", "description": "Analysis of death bowling."}

    def get_thread(self, comment_id):
        for t in self.threads:
            if t["comment_id"] == comment_id:
                return dict(t, own_replied=comment_id in self.posted_text_visible)
        return None

    def find_reply(self, parent_id, text):
        self.find_calls += 1
        return self.posted_text_visible.get(parent_id)

    def insert_reply(self, parent_id, text):
        if self.insert_error:
            err = self.insert_error
            if getattr(err, "post_anyway", False):         # timeout AFTER YouTube accepted the reply
                self.posted_text_visible[parent_id] = "REPLY_X"
            raise err
        self.inserted.append((parent_id, text))
        self.posted_text_visible[parent_id] = f"R{len(self.inserted)}"
        return f"R{len(self.inserted)}"


def thread(cid, text, video="vid1", author="UC_FAN", **kw):
    d = {"comment_id": cid, "video_id": video, "text": text, "author_name": "Fan", "author_channel_id": author,
         "published_at": "2026-10-01T10:00:00Z", "can_reply": True, "total_reply_count": 0, "replies": [],
         "own_replied": False, "is_own": False}
    d.update(kw)
    return d


def reset_db():
    """Clear the shared (temp) database used by agents.database.db."""
    from agents.database import db
    with db._conn() as conn:
        for table in ("topic_requests", "meta"):
            conn.execute(f"DELETE FROM {table}")


def make_service(channel="english", threads=None, llm=None, yt=None, real_topic_sink=False, **settings):
    path = tempfile.mktemp(suffix=".db")
    repo = Repository(Database(db_path=path))
    base = dict(enabled=True, tenant_id="default", publish_delay_seconds=0, semantic_check=True)
    base.update(settings)
    yt = yt or FakeYouTube(threads or [])
    llm = llm or FakeLLM()
    svc = CommentAgentService(channel, settings=Settings(**base), repository=repo, youtube=yt, llm=llm,
                              log=lambda *_: None,
                              topic_sink="default" if real_topic_sink else (lambda *a: svc.topics.append(a)))
    svc.topics = []
    return svc, repo, yt, llm
