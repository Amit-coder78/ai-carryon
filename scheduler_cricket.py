# scheduler_cricket.py
"""
Cricket pipeline: finished match -> summary -> script -> SEO -> voice ->
captions -> video -> upload. Deduplicates against output/cricket_posted.json.

Run locally: python scheduler_cricket.py
Deployed:    called by app_cricket.py's /trigger endpoint
"""
import os
import key_fallback
key_fallback.install()  # per-channel key first, shared key on 429/401/403
import json
from dotenv import load_dotenv
load_dotenv()

from agents_cricket.database import db as cricket_db, db_init_error as cricket_db_init_error

VIEW_TRACK_INTERVAL_SECONDS = 55 * 60  # ~hourly, throttled since /trigger fires every ~20 min
DAILY_UPLOAD_CAP = 5  # max cricket uploads per day, stored in cricket_db (survives restarts)


def run_cricket_cycle():
    import traceback
    try:
        return _run_cricket_cycle_inner()
    except Exception as e:
        print(f"CRICKET PIPELINE CRASHED: {e}")
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def _maybe_track_views():
    """Runs cricket view tracking at most once per VIEW_TRACK_INTERVAL_SECONDS,
    so every /trigger ping (every ~20 min) doesn't burn YouTube API quota."""
    from datetime import datetime, timezone

    try:
        last = cricket_db.get_meta("last_view_track_at")
        if last:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
            if elapsed < VIEW_TRACK_INTERVAL_SECONDS:
                return
        from agents_cricket.view_tracker_agent import track_views_cricket
        track_views_cricket()
        cricket_db.set_meta("last_view_track_at", datetime.now(timezone.utc).isoformat())
    except Exception as e:
        print(f"Cricket view tracking skipped: {e}")


def _check_daily_cap():
    """Returns today's upload count so far, resetting the counter if the
    stored date isn't today. Persisted in cricket_db so it survives Render
    restarts between /trigger calls."""
    from datetime import datetime, timezone
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        stored_date = cricket_db.get_meta("cricket_upload_date")
        count = int(cricket_db.get_meta("cricket_upload_count") or 0)
        if stored_date != today:
            cricket_db.set_meta("cricket_upload_date", today)
            cricket_db.set_meta("cricket_upload_count", "0")
            count = 0
        return count
    except Exception as e:
        print(f"Daily cap check failed, assuming 0: {e}")
        return 0


def _increment_daily_cap():
    from datetime import datetime, timezone
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        count = int(cricket_db.get_meta("cricket_upload_count") or 0) + 1
        cricket_db.set_meta("cricket_upload_date", today)
        cricket_db.set_meta("cricket_upload_count", str(count))
    except Exception as e:
        print(f"Daily cap increment failed: {e}")


def _run_cricket_cycle_inner():
    from agents_cricket.trending_agent import get_all_topics
    from agents_cricket.research_agent import get_summary_for_topic
    from agents_cricket.story_pipeline import create_story_script, record_story_meta
    from agents_cricket.seo_agent import generate_cricket_seo
    from agents_cricket.ab_title_agent import get_best_title_cricket
    from agents_cricket.saturation_agent import rank_topics_by_opportunity
    from agents_cricket.adaptive_scheduler import should_upload_now_cricket, mark_upload_done_cricket
    from agents_cricket.image_agent import generate_backgrounds
    from agents_cricket import video_clip_agent
    from agents_cricket.video_clip_agent import pick_clips
    from agents_cricket.upload_agent import upload_video
    from agents_cricket.voice_agent import generate_voice
    from agents.caption_agent import create_srt
    from agents_cricket.video_agent import create_video
    from datetime import datetime, timezone

    if cricket_db is None:
        return {"status": "error", "error": f"Cricket DB unavailable: {cricket_db_init_error}"}

    _maybe_track_views()

    from agents.run_state import RunState
    state = RunState.open("cricket", cricket_db.get_meta, cricket_db.set_meta)

    # Phase 4 — adaptive scheduling. Event-driven by design (see
    # agents_cricket/adaptive_scheduler.py docstring), so this only ever
    # blocks to enforce the minimum gap between uploads once real data exists.
    # A run that stopped half-way skips this gate and finishes first.
    if state.resumed:
        print("Adaptive scheduler: BYPASSED (finishing a run that stopped earlier)")
    else:
        upload_ok, upload_reason = should_upload_now_cricket()
        print(f"Adaptive scheduler: {upload_reason}")
        if not upload_ok:
            return {"status": "skipped_scheduler", "reason": upload_reason}

    if state.resumed:
        new_match = state.ctx["match"]
        print(f"Resuming match/topic: {new_match.get('name') or new_match.get('title', '')}")
    else:
        posted = cricket_db.get_all_posted_match_ids()
        topics = get_all_topics(limit=8)
        print(f"Found {len(topics)} topics (news/live/upcoming/finished)")

        candidates = [t for t in topics if t["id"] not in posted]
        if not candidates:
            print("No new topics to post.")
            return {"status": "no_new_match"}

        # Phase 1.5 — saturation ranking. Prefers less-covered matches when
        # several finished matches are available; news/live/upcoming always
        # sort first since they're time-sensitive and have nothing to check yet.
        try:
            candidates = rank_topics_by_opportunity(candidates)
        except Exception as e:
            print(f"Saturation ranking skipped: {e}")
        new_match = candidates[0]

    uploads_today = _check_daily_cap()
    if uploads_today >= DAILY_UPLOAD_CAP and not state.has("upload"):
        print(f"Daily cricket upload cap reached ({uploads_today}/{DAILY_UPLOAD_CAP}) — skipping.")
        return {"status": "daily_cap_reached", "uploads_today": uploads_today}

    if not state.resumed:
        state.begin({"match": new_match})

    topic_label = new_match.get("name") or new_match.get("title", "")
    print(f"Processing ({new_match.get('topic_type')}): {topic_label}")

    def _summary():
        s, st = get_summary_for_topic(new_match)
        return [s, st]

    summary, structured = state.step("summary", _summary, validate=lambda v: bool(v[0]))
    if not summary:
        print("Could not fetch summary — skipping this cycle.")
        state.forget("summary")
        return {"status": "summary_fetch_failed", "topic": topic_label}

    story = state.step("story", lambda: create_story_script(new_match, summary, structured))
    script = story["script"]
    print(f"Story: format={story['format']} hook_type={story['hook_type']} v2={story['used_v2']}")
    print(f"Script ({len(script.split())} words): {script[:80]}...")

    # Phase 3 — A/B title testing. Falls back to seo_agent's own single-shot
    # title (via title_override=None) if this errors, so a title-test bug
    # never blocks the whole pipeline.
    def _ab_title():
        r = get_best_title_cricket(summary, script, teams=structured.get("teams"))
        print(f"AB-tested title winner ({r['winner']['pattern']}, "
              f"score={r['winner']['score']}): {r['winner']['title']}")
        return r["winner"]["title"]

    try:
        winning_title = state.step("ab_title", _ab_title)
    except Exception as e:
        print(f"AB title test skipped: {e}")
        winning_title = None

    seo = state.step("seo", lambda: generate_cricket_seo(summary, script, title_override=winning_title))
    print(f"Title: {seo['title']}")

    def _voice():
        generate_voice(script, output_path="output/voice.mp3")
        return "output/voice.mp3"

    state.step("voice", _voice, validate=os.path.exists)

    def _captions():
        create_srt(script, audio_path="output/voice.mp3")
        return "output/captions.srt"

    state.step("captions", _captions, validate=os.path.exists, after=["voice"])

    # Hook Engine: topic-relevant opening clip, chosen BEFORE the normal clips. The cricket
    # text hook type (STAT/TACTICAL/...) steers the visual hook type. There is no licensed
    # match footage in this pipeline, so candidates come from Pexels and must clear the
    # 70% relevance gate: generic "cricket" footage for a player-specific story is rejected.
    def _hook():
        from agents.hook_engine import select_hook
        from agents_cricket.script_agent import safe_invoke as _hook_invoke
        _visual_type = {"STAT_HOOK": "curiosity", "TACTICAL_HOOK": "curiosity",
                        "MYSTERY_HOOK": "mystery", "EMOTIONAL_HOOK": "emotional",
                        "RECORD_HOOK": "shock"}.get(story.get("hook_type"))
        _ctx = "; ".join(x for x in (
            f"teams: {', '.join(structured.get('teams') or [])}" if structured.get("teams") else "",
            f"venue: {structured.get('venue')}" if structured.get("venue") else "",
            f"standout player: {structured.get('standout_player')}" if structured.get("standout_player") else "",
        ) if x)
        return select_hook(topic_label, script, "cricket", invoke=_hook_invoke,
                           meta_get=cricket_db.get_meta, meta_set=cricket_db.set_meta,
                           hint_hook_type=_visual_type, context=_ctx)

    hook = None
    try:
        hook = state.step("hook", _hook,
                          validate=lambda h: h is None or os.path.exists(h.get("path", "")))
        print(f"Hook: {hook['clip_id']} ({hook['duration']}s, relevance {hook['relevance']:.2f})"
              if hook else "Hook: none suitable - normal opening")
    except Exception as e:
        print(f"Hook engine skipped: {e}")

    def _clips():
        print("Pexels video clips fetch ho rahe hain...")
        paths, errors = pick_clips(structured, num_clips=4, script_words=len(script.split()))
        if len(paths) < 2:
            print(f"Too few Pexels clips ({errors}) — falling back to static images...")
            generate_backgrounds(summary, num_images=4, structured=structured)
            return [paths, False, []]
        return [paths, True, list(video_clip_agent.LAST_SELECTED)]

    clip_paths, use_pexels, last_selected = state.step(
        "clips", _clips,
        validate=lambda v: bool(v[1]) and bool(v[0]) and all(os.path.exists(p) for p in v[0]),
        after=["voice", "captions"])
    video_clip_agent.LAST_SELECTED = last_selected

    video_path = state.step(
        "video",
        lambda: create_video(use_pexels_clips=use_pexels,
                             hook=hook if use_pexels else None),  # writes to output/final_video.mp4
        validate=os.path.exists, after=["voice", "captions", "clips"])

    # Never uploads twice: once this step is saved, a resumed run skips it.
    video_id, video_url = state.step(
        "upload",
        lambda: list(upload_video(video_path, seo["title"], seo["description"], seo["hashtags"])))
    print(f"Uploaded: {video_url}")

    # V2 Phase 1 — only now (after a successful upload) put the clips on cooldown.
    try:
        from agents_cricket import asset_registry
        if use_pexels:
            asset_registry.record_usage(video_id, video_clip_agent.LAST_SELECTED)
    except Exception as e:
        print(f"Clip usage tracking skipped: {e}")

    if hook and use_pexels:
        try:
            from agents.hook_engine import record_usage as record_hook_usage
            record_hook_usage(hook, topic_label, "cricket", cricket_db.get_meta,
                              cricket_db.set_meta, video_id)
        except Exception as e:
            print(f"Hook usage tracking skipped: {e}")

    record_story_meta(story)

    cricket_db.mark_posted(new_match["id"], new_match.get("name") or new_match.get("title", ""))
    cricket_db.upsert_video(
        video_id=video_id,
        title=seo["title"],
        published=datetime.now(timezone.utc).isoformat(),
        match_id=new_match["id"],
    )
    _increment_daily_cap()
    mark_upload_done_cricket()
    try:
        # Also mark on the shared English/Hindi scheduler in case any other
        # code path still checks it there — harmless no-op for cricket.
        from agents.adaptive_scheduler import mark_upload_done
        mark_upload_done("cricket")
    except Exception:
        pass

    state.clear()
    return {"status": "uploaded", "video_url": video_url, "title": seo["title"],
            "format": story["format"], "hook_type": story["hook_type"]}


def run_comment_replies():
    """Comment agent for the cricket channel: sync + draft for owner review. Not part of the main
    cycle (comment fetching costs YouTube quota); trigger it from `--comments` or /trigger-comments.
    Needs COMMENT_AGENT_ENABLED=true. Publishes only owner-approved drafts, and only if the owner
    enabled publishing for this channel."""
    from comment_engine.config import load_settings
    from comment_engine.service import CommentAgentService
    settings = load_settings()
    if not settings.enabled:
        return {"status": "disabled", "hint": "set COMMENT_AGENT_ENABLED=true"}
    return CommentAgentService("cricket", settings=settings).run()


if __name__ == "__main__":
    import sys
    if "--comments" in sys.argv:
        result = run_comment_replies()
    else:
        result = run_cricket_cycle()
    print(result)
