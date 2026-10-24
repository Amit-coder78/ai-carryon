"""Owner CLI:  python -m comment_engine <command> --channel cricket [...]

Review loop:  run (drafts) -> drafts -> approve|edit|reject -> publish  (or next scheduled run).
"""
import argparse
import json
import sys

from .classifier import Classifier
from .config import PIPELINES, load_settings
from .context_retriever import ContextRetriever
from .persona_registry import PersonaRegistry
from .reply_generator import ReplyGenerator
from .reply_validator import ReplyValidator
from .schemas import Comment, VideoContext
from .service import AgentError, CommentAgentService


def _svc(a):
    return CommentAgentService(a.channel, tenant_id=a.tenant or None)


def _show(d):
    print(f"[{d['status']:<16}] {d['draft_id']}  comment={d['comment_id']}")
    print(f"    reply : {d['reply_text']!r}")
    if d.get("validation_reasons"):
        print(f"    flags : {d['validation_reasons']}")
    if d.get("review_reason"):
        print(f"    review: {d['review_reason']}")


def cmd_test(a):
    """Run a sample comment through classify -> generate -> validate. No DB writes, no YouTube."""
    from .channels import build_llm
    s = load_settings()
    llm = build_llm(a.channel)
    persona = PersonaRegistry().load(s.tenant_id, a.channel)
    comment = Comment(tenant_id=s.tenant_id, channel_id=a.channel, comment_id="cli-test", video_id="cli-video",
                      text=a.text)
    video = VideoContext(tenant_id=s.tenant_id, channel_id=a.channel, video_id="cli-video", title=a.title or "")
    analysis = Classifier(llm).classify(comment, video, persona)
    print("analysis:", analysis.model_dump_json())
    if analysis.reply_strategy in ("skip", "human_review"):
        print("routing :", analysis.reply_strategy)
        return
    draft = ReplyGenerator(llm).generate(comment, analysis, persona, video, persona_examples(persona, a.text))
    res = ReplyValidator(llm, semantic=s.semantic_check).validate(
        s.tenant_id, a.channel, comment, draft, persona, video, recent_replies=[])
    print("draft   :", draft.model_dump_json())
    print("validate:", res.model_dump_json())


def persona_examples(persona, text):
    return [{"comment": e.comment, "reply": e.reply} for e in persona.examples][:4]


def main(argv=None):
    p = argparse.ArgumentParser(prog="comment_engine")
    p.add_argument("command", choices=["run", "sync", "drafts", "approve", "reject", "edit", "publish", "feedback",
                                       "publishing", "pause", "resume", "persona", "metrics", "events", "test",
                                       "validate-personas", "purge"])
    p.add_argument("--channel", choices=PIPELINES)
    p.add_argument("--tenant", default="")
    p.add_argument("--draft")
    p.add_argument("--text")
    p.add_argument("--title")
    p.add_argument("--reason", default="")
    p.add_argument("--label")
    p.add_argument("--set", action="append", default=[], help="persona override key=value (repeatable)")
    p.add_argument("--on", choices=["on", "off"])
    p.add_argument("--status", action="append")
    p.add_argument("--days", type=int, default=90)
    a = p.parse_args(argv)

    if a.command == "validate-personas":
        for ch, per in PersonaRegistry().validate_all().items():
            print(f"{ch:<8} -> {per.persona_id} (v{per.version}, {per.language})")
        return 0
    if not a.channel:
        p.error("--channel is required")
    if a.command == "test":
        if not a.text:
            p.error("--text is required")
        cmd_test(a)
        return 0
    svc = _svc(a)
    try:
        if a.command == "run":
            print(json.dumps(svc.run(), indent=2, default=str))
        elif a.command == "sync":
            print("new comments:", svc.sync())
        elif a.command == "drafts":
            for d in svc.list_drafts(tuple(a.status) if a.status else
                                     ("pending_approval", "needs_review", "edited", "approved", "publish_failed")):
                _show(d)
        elif a.command == "approve":
            svc.approve(a.draft, actor="cli"); print("approved")
        elif a.command == "reject":
            svc.reject(a.draft, actor="cli", reason=a.reason); print("rejected")
        elif a.command == "edit":
            print(json.dumps(svc.edit_draft(a.draft, a.text or "", actor="cli"), indent=2))
        elif a.command == "publish":
            print("published reply id:", svc.publish(a.draft, actor="cli"))
        elif a.command == "feedback":
            svc.feedback(a.draft, a.label, actor="cli"); print("recorded")
        elif a.command == "publishing":
            svc.set_publishing(a.on == "on", actor="cli"); print("publishing", a.on)
        elif a.command in ("pause", "resume"):
            svc.set_paused(a.command == "pause", actor="cli"); print(a.command + "d")
        elif a.command == "persona":
            ov = {}
            for kv in a.set:
                k, _, v = kv.partition("=")
                ov[k] = int(v) if v.isdigit() else v
            if ov:
                svc.set_overrides(ov, actor="cli")
            per = svc.personas.load(svc.tenant_id, svc.channel_id)
            print(per.persona_id, "version", per.version, "| voice:", per.voice.model_dump_json())
        elif a.command == "metrics":
            print(json.dumps(svc.repo.metrics(svc.tenant_id, svc.channel_id), indent=2))
        elif a.command == "events":
            for e in svc.repo.list_events(svc.tenant_id, svc.channel_id, draft_id=a.draft)[:30]:
                print(e["created_at"], e["event_type"], e["actor"], e["detail"][:120])
        elif a.command == "purge":
            svc.repo.purge_older_than(svc.tenant_id, svc.channel_id, a.days); print("purged")
    except (AgentError, LookupError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
