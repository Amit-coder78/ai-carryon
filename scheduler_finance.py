"""AI CarryON Finance pipeline: research -> market data -> deterministic facts -> script (fact-checked) -> scenes
-> voice -> captions -> media (Pexels footage + verified charts) -> render -> review bundle / upload.

Mirrors scheduler_cricket.py: same RunState resume mechanism, same shared voice/caption/render calls, own state
tables (finance_*), own settings (FINANCE_*). Run locally: python scheduler_finance.py
"""
import importlib, json, os, shutil, traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from agents_finance import llm, market_data, research
from agents_finance.config import CHANNEL, FinanceSettings
from agents_finance.database import open_store
from agents_finance.fact_checker import check_script
from agents_finance.facts import Fact
from agents_finance.market_data import MarketDataError, StooqProvider, build_close_facts
from agents_finance.media import fetch_scene_media
from agents_finance.metadata import build_metadata
from agents_finance.scene_planner import Scene, plan_scenes
from agents_finance.script_agent import ScriptRejected, ScriptResult, generate_script
from agents_finance.topics import FinanceTopic, example_facts, requires_current_data, select_topic


def _load(spec, default):
    """'package.module:function' override via env, else the verified shared default."""
    if not spec: return default()
    mod, fn = spec.split(":")
    return getattr(importlib.import_module(mod), fn)


def _default_voice():
    from agents_cricket.voice_agent import generate_voice          # call shape verified in scheduler_cricket.py
    return generate_voice


def _default_captions():
    from agents.caption_agent import create_srt                    # shared; same call as Cricket
    return create_srt


def _default_video():
    from agents_cricket.video_agent import create_video            # call shape verified in scheduler_cricket.py
    return create_video


def _default_upload():
    from agents_finance.upload_agent import upload_video          # own OAuth token: FINANCE_TOKEN_JSON
    return upload_video


@dataclass
class Deps:
    store: object = None
    provider: object = field(default_factory=StooqProvider)
    invoke: object = llm.invoke
    voice: object = None
    captions: object = None
    video: object = None
    upload: object = None
    feeds: list = None
    clip_fn: object = None
    feed_fetch: object = research._default_fetch


def run_finance_cycle(settings=None, deps=None):
    try:
        return _run(settings or FinanceSettings.from_env(), deps or Deps())
    except Exception as e:
        print(f"FINANCE PIPELINE CRASHED: {e}")
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def _daily_count(store):
    today = datetime.now(timezone.utc).date().isoformat()
    if store.get_meta("finance_upload_date") != today:
        store.set_meta("finance_upload_date", today); store.set_meta("finance_upload_count", "0")
        return 0
    return int(store.get_meta("finance_upload_count") or 0)


def _build_facts(topic, settings, deps):
    facts = example_facts(topic)
    if topic.content_type == "news" and topic.sources:
        s = topic.sources[0]
        facts.append(Fact("news_headline", "Reported headline", topic.title, "historical", "text",
                          as_of=s["published_at"], source_url=s["url"], source_title=s["title"]))
    if settings.ticker:
        try:
            bars = deps.provider.daily_bars(settings.ticker, days=30)
            facts += build_close_facts("t", settings.ticker, settings.ticker, bars,
                                       "INR" if settings.market == "india" else "USD", deps.provider)
        except MarketDataError as e:
            print(f"Market data unavailable: {e}")
            if requires_current_data(settings):
                raise
    return [f.to_dict() for f in facts]


def _run(settings, deps):
    store = deps.store or open_store()
    from agents.run_state import RunState                      # shared resume mechanism used by Cricket
    state = RunState.open(CHANNEL, store.get_meta, store.set_meta)

    if state.resumed:
        topic = FinanceTopic.from_dict(state.ctx["topic"])
        print(f"Resuming: {topic.title}")
    else:
        if _daily_count(store) >= settings.daily_upload_cap:
            return {"status": "daily_cap_reached"}
        items = []
        if settings.content_type == "news":
            feeds = deps.feeds if deps.feeds is not None else research.feeds_from_env()
            items, errs = research.gather(settings, feeds, fetch=deps.feed_fetch)
            for e in errs: print("Research feed error:", e)
        topic = select_topic(settings, items, store.posted_ids())
        if topic is None:
            print(f"No unused topic for {settings.content_type}/{settings.category}: every title is already in finance_posted "
                  "(or news/analytical had nothing fresh). Try another FINANCE_CATEGORY or add titles to EVERGREEN.")
            return {"status": "no_grounded_topic"}
        state.begin({"topic": topic.to_dict()})
    print(f"Processing ({topic.content_type}/{topic.category}): {topic.title}")

    try:
        facts_d = state.step("facts", lambda: _build_facts(topic, settings, deps), validate=lambda v: isinstance(v, list))
    except MarketDataError as e:
        state.clear()
        return {"status": "data_unavailable", "reason": str(e)}   # never publish current-market content without data
    facts = [Fact.from_dict(d) for d in facts_d]

    try:
        script_d = state.step("script", lambda: generate_script(topic, facts, settings, deps.invoke).to_dict())
    except ScriptRejected as e:
        state.clear()
        return {"status": "fact_check_failed", "reason": str(e)}
    script = ScriptResult(**script_d)
    print(f"Script ({len(script.narration.split())} words): {script.narration[:80]}...")

    scenes_d = state.step("scenes", lambda: [s.to_dict() for s in plan_scenes(script.template, facts, topic, settings, script.visual_hints)])
    scenes = [Scene.from_dict(d) for d in scenes_d]
    meta = state.step("metadata", lambda: build_metadata(topic, script, facts, settings))

    voice = _load(os.getenv("FINANCE_VOICE_IMPL"), _default_voice) if deps.voice is None else deps.voice
    captions = _load(os.getenv("FINANCE_CAPTIONS_IMPL"), _default_captions) if deps.captions is None else deps.captions
    video = _load(os.getenv("FINANCE_VIDEO_IMPL"), _default_video) if deps.video is None else deps.video
    os.makedirs("output", exist_ok=True)

    state.step("voice", lambda: (voice(script.narration, output_path="output/voice.mp3"), "output/voice.mp3")[1], validate=os.path.exists)
    if settings.subtitles:
        state.step("captions", lambda: (captions(script.narration, audio_path="output/voice.mp3"), "output/captions.srt")[1],
                   validate=os.path.exists, after=["voice"])

    def _media():
        paths, errors = fetch_scene_media(scenes, clip_fn=deps.clip_fn, store=store)
        for e in errors: print("Media:", e)
        return paths
    state.step("media", _media, validate=lambda p: len(p) >= 2 and all(os.path.exists(x) for x in p), after=["voice"])

    video_path = state.step("video", lambda: (video(use_pexels_clips=True, hook=None), "output/final_video.mp4")[1],
                            validate=os.path.exists, after=["voice", "media"])

    if settings.require_review:
        bundle = os.path.join("output", "finance_review", topic.id.replace(":", "_"))
        os.makedirs(bundle, exist_ok=True)
        shutil.copy(video_path, os.path.join(bundle, "video.mp4"))
        for name, obj in (("metadata.json", meta), ("facts.json", facts_d), ("scenes.json", scenes_d), ("script.json", script_d)):
            with open(os.path.join(bundle, name), "w") as f: json.dump(obj, f, indent=2, ensure_ascii=False)
        store.mark_posted(topic.id, topic.title)
        state.clear()
        return {"status": "ready_for_review", "bundle": bundle, "title": meta["title"], "warnings": script.warnings}

    upload = _load(os.getenv("FINANCE_UPLOAD_IMPL"), _default_upload) if deps.upload is None else deps.upload
    vid, url = state.step("upload", lambda: list(upload(video_path, meta["title"], meta["description"], meta["hashtags"])))
    store.mark_posted(topic.id, topic.title)
    store.set_meta("finance_upload_count", str(_daily_count(store) + 1))
    state.clear()
    return {"status": "uploaded", "video_url": url, "title": meta["title"]}


def _comment_agent_job():
    """Comment agent (drafts for owner review; gated by COMMENT_AGENT_* settings). Never raises."""
    try:
        from comment_engine.service import run_scheduled
        run_scheduled("finance")
    except Exception as e:
        print(f"Comment agent job error: {e}")


if __name__ == "__main__":
    print(run_finance_cycle())
    _comment_agent_job()
