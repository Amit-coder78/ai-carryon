# scheduler_gaming.py
"""
Gaming pipeline: Twitch clip -> transcript -> moment analysis -> script -> SEO ->
voice lines + edit plan -> render (game audio kept) -> upload. Deduplicates against gaming_posted_clips in the DB.

Run locally: python scheduler_gaming.py
Deployed:    .github/workflows/gaming-scheduler.yml (cron, no server needed
             — unlike cricket's Render /trigger pattern, this runs the
             script directly as a scheduled GitHub Actions job).
"""
import os
import key_fallback
key_fallback.install()  # per-channel key first, shared key on 429/401/403
from dotenv import load_dotenv
load_dotenv()

from agents_gaming.database import db as gaming_db, db_init_error as gaming_db_init_error

DAILY_UPLOAD_CAP = int(os.getenv("GAMING_DAILY_UPLOAD_CAP", "5"))

# Gaming V2 (see AI_CarryON_Gaming_Pipeline_V2_Update.md). Set GAMING_V2_ENABLED=0
# to run the legacy flow (most-viewed clip -> single-prompt script) unchanged.
GAMING_V2_ENABLED = os.getenv("GAMING_V2_ENABLED", "1") != "0"
ANALYZE_TOP_K = int(os.getenv("GAMING_ANALYZE_TOP_K", "3"))     # clips downloaded + analysed per run
MAX_CLIP_TRIES = int(os.getenv("GAMING_MAX_CLIP_TRIES", "2"))   # clips whose scripts we try before giving up
# Upper bound on clips downloaded + vision-analysed per run while looking for ANALYZE_TOP_K usable
# (real gameplay) ones. Each analysed clip is one Gemini vision call, so this also caps the quota spend.
MAX_ANALYZED = int(os.getenv("GAMING_MAX_ANALYZED", "5"))
REJECTED_KEY = "gaming_rejected_clips"
# Gaming V2 Sprint 2. "v2" = keep the clip's own audio, short voice-overlay lines placed around the
# payoff, ducking, punch-in/freeze edits, safe-zone captions (agents_gaming/producer.py).
# "legacy" = the old flow: one long voiceover over silent footage via the shared renderer.
AUDIO_MODE = os.getenv("GAMING_AUDIO_MODE", "v2").lower()
NARRATION_MODE = "short" if AUDIO_MODE == "v2" else "full"
# Comma-separated Twitch language codes to prefer (empty = no preference).
CLIP_LANGUAGES = [l for l in os.getenv("GAMING_CLIP_LANGUAGES", "en").split(",") if l.strip()]

# moment type -> existing seo_agent title pattern (full SEO upgrade is V2 Phase 14)
SEO_PATTERN_BY_MOMENT = {
    "CLUTCH": "clutch", "INSANE_PLAY": "clutch",
    "FAIL": "funny", "FUNNY": "funny", "TROLL": "funny",
    "RECORD": "record",
    "REACTION": "reaction", "RAGE": "reaction", "LUCK": "reaction",
    "UNEXPECTED": "reaction", "DRAMA": "reaction",
}


def _load_rejected():
    import json
    try:
        raw = gaming_db.get_meta(REJECTED_KEY)
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _add_rejected(clip_id):
    """Remember clips whose scripts failed the quality bar so the next cycle
    moves on instead of re-picking the same clip forever (bounded to 50)."""
    import json
    try:
        ids = list(_load_rejected()) + [clip_id]
        gaming_db.set_meta(REJECTED_KEY, json.dumps(ids[-50:]))
    except Exception as e:
        print(f"Could not record rejected clip: {e}")


def _caption_text(script):
    """Script minus the '...' pause markers — TTS uses them for pauses, but
    they must not show up as caption words."""
    import re
    return re.sub(r"\.{2,}|\u2026", " ", script)


def _analyse_with_refine(c, tr, clip_path, analyze_moment, refine_key_timestamp):
    """Moment analysis + audio-peak refinement as ONE saveable step."""
    m = analyze_moment({**c, "_transcript_text": tr["text"]} if tr else c, clip_path)
    if m.get("source") == "vision":
        # The vision tier only sees 5 frames, so its payoff time can be off by a second or more.
        # Snap it to the loudest burst nearby (the payoff is nearly always the loudest moment).
        new_key, changed = refine_key_timestamp(clip_path, m.get("key_timestamp"))
        if changed:
            print(f"Payoff time refined by audio peak: {m.get('key_timestamp')}s -> {new_key}s")
            m["key_timestamp"], m["key_refined"] = new_key, True
    return m


def _select_and_script_v2(candidates, posted, state=None):
    """V2 front half: score -> download+analyse top K -> re-score with real
    moment quality -> hook/commentary/quality loop on the best clips.
    Returns {"status": "ok", ...} or a terminal status dict for the scheduler."""
    import shutil
    from agents_gaming.trending_agent import FOLLOWED_STREAMERS
    from agents_gaming.clip_scorer import prefer_languages, rank_clips_v2
    from agents_gaming.moment_analyzer import analyze_moment
    from agents_gaming.audio_peak import refine_key_timestamp
    from agents_gaming.transcript_agent import transcribe_clip
    from agents_gaming.video_clip_agent import download_twitch_clip
    from agents_gaming.research_agent import get_summary_for_clip
    from agents_gaming.script_agent import create_gaming_script_v2

    candidates = prefer_languages(candidates, CLIP_LANGUAGES, min_keep=ANALYZE_TOP_K)
    rejected = _load_rejected()
    shortlist = rank_clips_v2(candidates, posted, rejected, followed=FOLLOWED_STREAMERS,
                              limit=max(MAX_ANALYZED, ANALYZE_TOP_K))
    if not shortlist:
        print("All candidate clips already posted or rejected this cycle.")
        return {"status": "no_new_clip"}

    folder = "assets/gaming_clips"
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder, exist_ok=True)

    moments, paths, transcripts = {}, {}, {}
    usable, non_gameplay = 0, 0
    for c in shortlist:
        if usable >= ANALYZE_TOP_K:
            break      # enough real-gameplay clips; don't spend more downloads / vision calls
        try:
            paths[c["id"]] = download_twitch_clip(c, os.path.join(folder, f"{c['id']}.mp4"))
        except Exception as e:
            print(f"Clip {c.get('id')} download failed, skipping: {e}")
            continue
        _cp = paths[c["id"]]
        _step = (lambda name, fn: state.step(name, fn)) if state is not None else (lambda name, fn: fn())
        # Saved per clip: a stopped run does not pay for the transcript / vision call again.
        tr = _step(f"transcript:{c['id']}", lambda: transcribe_clip(_cp)) if AUDIO_MODE == "v2" else None
        if tr:
            transcripts[c["id"]] = tr
        moments[c["id"]] = _step(
            f"moment:{c['id']}",
            lambda: _analyse_with_refine(c, tr, _cp, analyze_moment, refine_key_timestamp))
        m = moments[c["id"]]
        print(f"Analysed '{c.get('title')}': {m['moment_type']} intensity={m['intensity']} "
              f"[{m['source']}] (metadata score {c['_score']})")
        if m.get("is_gameplay") is False:
            # Category says "game" but the footage is IRL / webcam / lobby — not a gaming Short.
            print(f"Clip {c['id']} is not gameplay footage (game='{m.get('game', '')}', "
                  f"what='{str(m.get('what_happened', ''))[:100]}') — skipping and remembering it")
            paths.pop(c["id"], None)
            moments.pop(c["id"], None)
            transcripts.pop(c["id"], None)
            _add_rejected(c["id"])
            non_gameplay += 1
        else:
            usable += 1
    if not paths:
        reason = ("every analysed clip was non-gameplay footage" if non_gameplay
                  else "no clip could be downloaded")
        print(f"No usable clip this cycle: {reason}")
        return {"status": "no_new_clip", "reason": reason}

    # Re-score against the FULL pool (percentiles stay meaningful), now with
    # real moment quality for the analysed clips; keep only clips we hold footage for.
    full = rank_clips_v2(candidates, posted, rejected, moments=moments, followed=FOLLOWED_STREAMERS)
    ranked = [c for c in full if c["id"] in paths]

    for clip in ranked[:MAX_CLIP_TRIES]:
        moment = moments[clip["id"]]
        print(f"Trying clip '{clip.get('title')}' by {clip.get('broadcaster_name')} "
              f"(V2 score {clip['_score']}, parts {clip['_score_parts']})")
        if state is not None:
            summary, structured = state.step(f"summary:{clip['id']}", lambda: list(get_summary_for_clip(clip)))
        else:
            summary, structured = get_summary_for_clip(clip)
        structured["game"] = structured.get("game") or moment.get("game", "")
        tr = transcripts.get(clip["id"])
        if tr:
            summary += f"\nStreamer speech (auto-transcript, may contain errors): {tr['text']}"
        result = create_gaming_script_v2(clip, moment, summary, structured, db=gaming_db,
                                         mode=NARRATION_MODE, state=state)
        if result and result.get("unjudged"):
            # Reviewer outage, not a bad clip: don't burn the clip, don't try others.
            return {"status": "judge_unavailable"}
        if result and result["passed"]:
            return {"status": "ok", "clip": clip, "moment": moment, "summary": summary,
                    "structured": structured, "script": result["script"], "hook": result["hook"],
                    "clip_path": paths[clip["id"]], "transcript": tr}
        print(f"Clip {clip.get('id')} rejected: script never cleared the quality bar")
        _add_rejected(clip["id"])

    return {"status": "quality_rejected"}


def run_gaming_cycle(force=False):
    import traceback
    try:
        return _run_gaming_cycle_inner(force=force)
    except Exception as e:
        print(f"GAMING PIPELINE CRASHED: {e}")
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def _maybe_track_views():
    """Runs gaming view tracking at most once per hour, so every scheduler
    run doesn't burn YouTube API quota (same throttle pattern as cricket)."""
    from datetime import datetime, timezone
    VIEW_TRACK_INTERVAL_SECONDS = 55 * 60
    try:
        last = gaming_db.get_meta("last_view_track_at")
        if last:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
            if elapsed < VIEW_TRACK_INTERVAL_SECONDS:
                return
        from agents_gaming.view_tracker_agent import track_views_gaming
        track_views_gaming()
        gaming_db.set_meta("last_view_track_at", datetime.now(timezone.utc).isoformat())
    except Exception as e:
        print(f"Gaming view tracking skipped: {e}")


def _check_daily_cap():
    from datetime import datetime, timezone
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        stored_date = gaming_db.get_meta("gaming_upload_date")
        count = int(gaming_db.get_meta("gaming_upload_count") or 0)
        if stored_date != today:
            gaming_db.set_meta("gaming_upload_date", today)
            gaming_db.set_meta("gaming_upload_count", "0")
            count = 0
        return count
    except Exception as e:
        print(f"Daily cap check failed, assuming 0: {e}")
        return 0


def _increment_daily_cap():
    from datetime import datetime, timezone
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        count = int(gaming_db.get_meta("gaming_upload_count") or 0) + 1
        gaming_db.set_meta("gaming_upload_date", today)
        gaming_db.set_meta("gaming_upload_count", str(count))
    except Exception as e:
        print(f"Daily cap increment failed: {e}")


def _run_gaming_cycle_inner(force=False):
    from datetime import datetime, timezone

    from agents_gaming.trending_agent import get_all_topics
    from agents_gaming.clip_finder import find_best_unposted_clip
    from agents_gaming.research_agent import get_summary_for_clip
    from agents_gaming.script_agent import create_gaming_script
    from agents_gaming.seo_agent import generate_seo
    from agents_gaming.video_clip_agent import get_gaming_background_clip
    from agents_gaming.upload_agent import upload_video

    from agents.voice_agent import generate_voice
    from agents.caption_agent import create_srt
    from agents.video_agent import _create_video_from_pexels_clips
    from agents_gaming.adaptive_scheduler import should_upload_now_gaming, mark_upload_done_gaming

    if gaming_db is None:
        return {"status": "error", "error": f"Gaming DB unavailable: {gaming_db_init_error}"}

    from agents.model_invoke_agent_english import reset_groq_budget
    reset_groq_budget()

    _maybe_track_views()

    from agents.run_state import RunState
    state = RunState.open("gaming", gaming_db.get_meta, gaming_db.set_meta)

    if force:
        print("Adaptive scheduler: BYPASSED (forced run from dashboard)")
    elif state.resumed:
        print("Adaptive scheduler: BYPASSED (finishing a run that stopped earlier)")
    else:
        upload_ok, upload_reason = should_upload_now_gaming()
        print(f"Adaptive scheduler: {upload_reason}")
        if not upload_ok:
            return {"status": "skipped_scheduler", "reason": upload_reason}

    uploads_today = _check_daily_cap()
    if uploads_today >= DAILY_UPLOAD_CAP:
        print(f"Daily gaming upload cap reached ({uploads_today}/{DAILY_UPLOAD_CAP}) — skipping.")
        return {"status": "daily_cap_reached", "uploads_today": uploads_today}

    posted = gaming_db.get_all_posted_clip_ids()
    moment, hook, clip_paths, transcript = None, None, None, None

    saved_sel = state.get("selected") if (GAMING_V2_ENABLED and state.resumed) else None
    if saved_sel:
        # The stopped run had already finished the clip + script. Only the footage
        # (disk was wiped) has to be downloaded again; no LLM call is repeated.
        from agents_gaming.video_clip_agent import download_twitch_clip
        clip, moment, hook = saved_sel["clip"], saved_sel["moment"], saved_sel["hook"]
        structured, script = saved_sel["structured"], saved_sel["script"]
        transcript = saved_sel.get("transcript")
        folder = "assets/gaming_clips"
        os.makedirs(folder, exist_ok=True)
        clip_path = os.path.join(folder, f"{clip['id']}.mp4")
        if not os.path.exists(clip_path):
            clip_path = download_twitch_clip(clip, clip_path)
        clip_paths = [clip_path]
        print(f"Resuming clip '{clip.get('title')}' with the saved script — skipping scoring and script writing")
    elif GAMING_V2_ENABLED:
        candidates = get_all_topics()
        if not candidates:
            print("No candidate clips found this cycle (check TWITCH_FOLLOWED_STREAMERS / "
                  "TWITCH_TRACKED_GAMES and Twitch app credentials).")
            state.clear()
            return {"status": "no_candidates"}
        if not state.resumed:
            state.begin({})
        sel = _select_and_script_v2(candidates, posted, state=state)
        if sel["status"] != "ok":
            if sel["status"] != "judge_unavailable":
                state.clear()      # nothing worth resuming; judge outage keeps its saved work
            return sel
        clip, moment, hook = sel["clip"], sel["moment"], sel["hook"]
        structured, script = sel["structured"], sel["script"]
        clip_paths = [sel["clip_path"]]
        transcript = sel.get("transcript")
        state.set("selected", {"clip": clip, "moment": moment, "hook": hook, "structured": structured,
                               "script": script, "transcript": transcript})
        print(f"Selected clip: '{clip.get('title')}' by {clip.get('broadcaster_name')} "
              f"-> {moment['moment_type']} (V2 score {clip['_score']})")
    else:
        candidates = get_all_topics()
        if not candidates:
            return {"status": "no_candidates"}
        clip = find_best_unposted_clip(candidates, posted)
        if not clip:
            print("All candidate clips already posted this cycle.")
            return {"status": "no_new_clip"}
        print(f"Selected clip: '{clip.get('title')}' by {clip.get('broadcaster_name')} "
              f"({clip.get('view_count', 0):,} views)")
        summary, structured = get_summary_for_clip(clip)
        script = create_gaming_script(summary, structured)

    print(f"Script ({len(script.split())} words): {script[:80]}...")

    seo_pattern = SEO_PATTERN_BY_MOMENT.get(moment["moment_type"]) if moment else None
    title, description, hashtags = state.step(
        "seo", lambda: list(generate_seo(structured, _caption_text(script), use_pattern=seo_pattern)))
    print(f"Title: {title}")

    use_v2_audio = GAMING_V2_ENABLED and AUDIO_MODE == "v2"
    if not use_v2_audio:
        generate_voice(script, output_path="output/voice.mp3")
        create_srt(_caption_text(script), audio_path="output/voice.mp3")

    if clip_paths is None:
        print("Downloading Twitch clip footage...")
        clip_paths = get_gaming_background_clip(clip)

    # Hook Engine (gaming): cold-open on the real gameplay's peak moment. Only when the
    # vision tier located it with high intensity; otherwise None -> renders as before.
    # (named visual_hook: `hook` above is the TEXT hook that feeds remember_script().)
    visual_hook = None
    if moment:
        try:
            from agents_gaming.gameplay_hook import select_gameplay_hook
            visual_hook = select_gameplay_hook(clip_paths[0], moment, clip)
        except Exception as e:
            print(f"Hook engine skipped: {e}")

    if use_v2_audio:
        # Sprint 2: game audio kept + ducked, short voice lines around the payoff, punch-in/freeze,
        # safe-zone captions. A failed render means NO upload (never publish a degraded video);
        # GAMING_AUDIO_MODE=legacy switches to the old renderer.
        try:
            from agents_gaming.producer import produce_video
            video_path = state.step(
                "video",
                lambda: produce_video(clip_paths[0], script, hook, moment, transcript, cold_open=visual_hook),
                validate=os.path.exists)
        except Exception as e:
            print(f"Gaming render failed — not uploading: {e}")
            return {"status": "render_failed", "error": str(e)}
    else:
        # Legacy: the shared renderer drops the clip's own audio (-an) and sizes the video to the
        # voiceover. `hook` is only passed when there is one, so a no-hook run makes exactly the
        # same call as before the Hook Engine existed.
        video_path = _create_video_from_pexels_clips(
            clip_paths, "output/voice.mp3", "output/captions.srt",
            music_path=None, **({"hook": visual_hook} if visual_hook else {}),
        )

    # Never uploads twice: once this step is saved, a resumed run skips it.
    video_id, video_url = state.step(
        "upload", lambda: list(upload_video(video_path, title, description, hashtags)))
    print(f"Uploaded: {video_url}")

    if visual_hook:
        try:
            from agents.hook_engine import record_usage as record_hook_usage
            record_hook_usage(visual_hook, clip.get("title", ""), "gaming", gaming_db.get_meta,
                              gaming_db.set_meta, video_id)   # gameplay hooks: log only
        except Exception as e:
            print(f"Hook logging skipped: {e}")

    gaming_db.mark_posted(clip["id"], clip.get("title", ""), clip.get("broadcaster_name", ""))
    if GAMING_V2_ENABLED:
        from agents_gaming.script_agent import remember_script
        remember_script(gaming_db, script, (hook or {}).get("type"))
    gaming_db.upsert_video(
        video_id=video_id,
        title=title,
        published=datetime.now(timezone.utc).isoformat(),
        clip_id=clip["id"],
    )
    _increment_daily_cap()
    mark_upload_done_gaming()
    state.clear()

    return {"status": "uploaded", "video_url": video_url, "title": title}


def _comment_agent_job():
    """Comment agent (drafts for owner review; gated by COMMENT_AGENT_* settings). Never raises."""
    try:
        from comment_engine.service import run_scheduled
        run_scheduled("gaming")
    except Exception as e:
        print(f"Comment agent job error: {e}")


if __name__ == "__main__":
    result = run_gaming_cycle()
    print(result)
    _comment_agent_job()
