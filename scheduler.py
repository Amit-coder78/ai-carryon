"""
scheduler.py — single-run entrypoint for Cloud Run Jobs.

Previously an infinite loop (`schedule` + `while True` + `time.sleep(30)`)
that polled every 30s inside an always-on Railway worker. Cloud Run Jobs
don't work that way — Cloud Scheduler triggers a fresh container once per
interval, it runs to completion, and exits. So the polling loop is gone;
this file now does ONE pass per invocation:

  1. Cleanup sweep
  2. View tracking + AB-loop closing (every invocation)
  3. Comment replies (every invocation)
  4. Generation — ONLY if the current UTC hour is a top-velocity slot AND
     the daily cap hasn't been hit (same logic as before, just checked
     once instead of polled)

Deploy Cloud Scheduler to trigger this hourly (matches the old 30s-poll's
effective hourly granularity, since top-hour slots are whole hours anyway).
See DEPLOY.md for the exact `gcloud scheduler jobs create` command.

Also removed: the GitHub JSON restore/backup dance (agents/data_persistence)
that used to run on every startup — that existed because Railway/Render's
disk wasn't reliably persistent. Firestore is persistent by default, so
none of that is needed anymore.
"""

import os
import key_fallback
key_fallback.install()  # per-channel key first, shared key on 429/401/403
import time
import datetime


LOG_FILE = "output/scheduler_log.txt"

VIDEOS_PER_DAY = int(os.environ.get("VIDEOS_PER_DAY", "3"))  # daily upload quota


def log(message):
    os.makedirs("output", exist_ok=True)
    utc_now = datetime.datetime.utcnow()
    ist_now = utc_now + datetime.timedelta(hours=5, minutes=30)
    timestamp = ist_now.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp} IST] {message}"
    print(entry, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(entry + "\n")


def get_recent_topics(hours=24):
    from agents.database import db
    return db.get_recent_posted(hours=hours, channel="english")


def mark_posted(topic):
    from agents.database import db
    try:
        db.mark_posted(topic, channel="english")
    except Exception as e:
        log(f"DB mark_posted error: {e}")


def get_fresh_trending_topic(region_code="US", max_attempts=5):
    from agents.trending_agent import get_trending_topic
    from agents.saturation_agent import check_saturation

    recent_topics = get_recent_topics(hours=24)

    # Brain platform ranking (off unless BRAIN_ENGINE_URL etc. are set). Returns None for every
    # non-happy path, in which case the original selection below runs exactly as before.
    from agents.topic_selection import brain_ranked_topic
    brain_topic = brain_ranked_topic("english", region_code, recent_topics=recent_topics,
                                     max_attempts=max_attempts, log=log)
    if brain_topic:
        return brain_topic

    for attempt in range(max_attempts):
        topic = get_trending_topic(region_code=region_code)

        if topic.lower().strip() in recent_topics:
            log(f"Topic already posted in last 24h, retrying ({attempt+1}/{max_attempts}): {topic}")
            continue

        saturation = check_saturation(topic)
        log(f"Saturation check: score={saturation['opportunity_score']} — {saturation['reason']}")

        if not saturation["proceed"]:
            log(f"Topic too saturated, retrying ({attempt+1}/{max_attempts}): {topic}")
            continue

        return topic

    return topic + " - extra"


# NOTE: The schedule-gate logic used to live here (get_top_upload_hours /
# should_upload_now), but main() never called it — it calls
# should_upload_now_for_channel() from agents/adaptive_scheduler.py instead.
# The two copies had different fallback hours ([4, 12, 19] here vs
# [6, 12, 18] there) and different log wording, so editing this file did
# nothing in production. Removed to avoid that trap. The single source of
# truth for peak-hour / fallback-hour logic is now only in
# agents/adaptive_scheduler.py — edit should_upload_now_for_channel() there.


def run_generation_pipeline(topic_override=None):
    """The actual content pipeline — unchanged from the original, aside from
    the lock now being Firestore-based instead of a local lock file (a local
    file wouldn't be visible to any other Cloud Run Job execution anyway)."""
    from agents.database import db

    acquired, age = db.try_acquire_lock("generation_english", ttl_seconds=1800)
    if not acquired:
        log(f"Generation already in progress elsewhere (lock age {age:.0f}s) — skipping to avoid concurrent run")
        return

    log("=== Starting scheduled video generation ===")
    try:
        from agents.checkpoint import (
            save_checkpoint, is_stage_done, get_stage_data,
            clear_checkpoint, list_checkpoints, note_resume,
        )
        from agents.research_agent import research
        from agents.script_agent import create_script
        from agents.seo_agent import generate_seo
        from agents.thumbnail_agent import generate_thumbnail_text
        from agents.thumbnail_generator import generate_thumbnail
        from agents.image_agent import generate_backgrounds
        from agents.video_clip_agent import generate_background_clips
        from agents.voice_agent import generate_voice
        from agents.caption_agent import create_srt
        from agents.video_agent import create_video
        from agents.upload_agent import upload_video

        pending = list_checkpoints()
        if pending:
            cp = pending[0]
            topic = cp["topic"]
            note_resume(topic)
            log(f"Resuming incomplete generation for topic: {topic} (last stage: {cp.get('last_stage')})")
        elif topic_override:
            topic = topic_override
        else:
            log("Fetching trending YouTube topic...")
            topic = get_fresh_trending_topic(region_code="US")
            log(f"Trending topic: {topic}")

        if is_stage_done(topic, "research"):
            research_data = get_stage_data(topic, "research")
            log("Resuming: research already done")
        else:
            log("Researching...")
            research_data = research(topic)
            save_checkpoint(topic, "research", research_data)

        if is_stage_done(topic, "script"):
            script = get_stage_data(topic, "script")
            log("Resuming: script already done")
        else:
            log("Writing script...")
            script = create_script(research_data)
            save_checkpoint(topic, "script", script)

        if is_stage_done(topic, "seo"):
            seo = get_stage_data(topic, "seo")
            log("Resuming: seo already done")
        else:
            log("Generating SEO...")
            seo = generate_seo(topic, script)
            save_checkpoint(topic, "seo", seo)

        log("Generating thumbnail text...")
        generate_thumbnail_text(topic)

        if is_stage_done(topic, "thumbnail") and get_stage_data(topic, "thumbnail") and os.path.exists(get_stage_data(topic, "thumbnail")):
            thumbnail_image = get_stage_data(topic, "thumbnail")
            log("Resuming: thumbnail already done")
        else:
            log("Generating thumbnail image...")
            thumbnail_image = generate_thumbnail(seo["title"], topic)
            save_checkpoint(topic, "thumbnail", thumbnail_image)

        # Hook Engine: choose a topic-relevant opening clip BEFORE the normal clips.
        # Never blocks the run: no suitable hook -> hook=None -> renders as before.
        hook = None
        try:
            from agents.hook_engine import select_hook
            if is_stage_done(topic, "hook"):
                hook = get_stage_data(topic, "hook")
                if hook and not os.path.exists(hook.get("path", "")):
                    hook = None
                    is_hook_cached = False
                else:
                    is_hook_cached = True
                    log("Resuming: hook stage already done")
            else:
                is_hook_cached = False
            if not is_hook_cached:
                log("Selecting hook clip...")
                from agents.model_invoke_agent_english import safe_invoke as _hook_invoke
                hook = select_hook(topic, script, "english", invoke=_hook_invoke,
                                   meta_get=db.get_meta, meta_set=db.set_meta)
                save_checkpoint(topic, "hook", hook)
            log(f"Hook: {hook['clip_id']} ({hook['duration']}s, relevance {hook['relevance']:.2f})"
                if hook else "Hook: none suitable - normal opening")
        except Exception as hook_err:
            log(f"Hook engine skipped: {hook_err}")
            hook = None

        images_regenerated = False
        if is_stage_done(topic, "images") and get_stage_data(topic, "images") and all(os.path.exists(p) for p in get_stage_data(topic, "images")):
            image_paths = get_stage_data(topic, "images")
            log("Resuming: images already done")
            use_pexels = bool(image_paths) and image_paths[0].endswith(".mp4")
        else:
            images_regenerated = True
            log("Fetching dynamic Pexels video clips...")
            image_paths, image_errors = generate_background_clips(topic, script, num_clips=4)
            if len(image_paths) < 2:
                log(f"Too few Pexels clips ({image_errors}) — falling back to static images...")
                image_paths, image_errors = generate_backgrounds(topic, script, num_images=4)
                use_pexels = False
            else:
                use_pexels = True
            if not image_paths:
                log(f"ERROR: No images — {image_errors}")
                return
            save_checkpoint(topic, "images", image_paths)

        # Files live on local disk, which may be wiped between runs. If a file is gone,
        # that step AND every step after it (which read it) must be made again.
        media_redone = images_regenerated or not (image_paths and all(os.path.exists(p) for p in image_paths))

        if (not media_redone) and is_stage_done(topic, "voice") and get_stage_data(topic, "voice") and os.path.exists(get_stage_data(topic, "voice")):
            voice_file = get_stage_data(topic, "voice")
            log("Resuming: voice already done")
        else:
            media_redone = True
            log("Generating voiceover...")
            voice_file = generate_voice(script)
            save_checkpoint(topic, "voice", voice_file)

        if (not media_redone) and is_stage_done(topic, "captions") and os.path.exists("output/captions.srt"):
            log("Resuming: captions already done")
        else:
            media_redone = True
            log("Generating captions...")
            create_srt(script, voice_file)
            save_checkpoint(topic, "captions", True)

        if (not media_redone) and is_stage_done(topic, "video") and get_stage_data(topic, "video") and os.path.exists(get_stage_data(topic, "video")):
            video_file = get_stage_data(topic, "video")
            log("Resuming: video already rendered")
        else:
            log("Creating video...")
            video_file = create_video(use_pexels_clips=use_pexels,
                                      hook=hook if use_pexels else None)
            save_checkpoint(topic, "video", video_file)

        if is_stage_done(topic, "upload"):
            video_id, video_url = get_stage_data(topic, "upload")
            log(f"Resuming: already uploaded previously — {video_url} — skipping re-upload")
        else:
            log("Uploading to YouTube...")
            video_id, video_url = upload_video(
                video_path=video_file,
                title=seo["title"],
                description=seo["description"],
                hashtags=seo["hashtags"],
                thumbnail_path=thumbnail_image
            )
            save_checkpoint(topic, "upload", [video_id, video_url])
            if hook and use_pexels:
                from agents.hook_engine import record_usage as record_hook_usage
                record_hook_usage(hook, topic, "english", db.get_meta, db.set_meta, video_id)

        if seo.get("ab_winner"):
            try:
                linked = db.link_ab_test_to_video(seo["ab_winner"], video_id)
                if linked:
                    log(f"Linked AB test to video_id {video_id}")
            except Exception as link_err:
                log(f"AB test link skipped: {link_err}")

        log("Posting to Instagram Reels...")
        try:
            from agents.instagram_agent import post_reel
            caption = f"{seo['title']}\n\n{seo['description']}\n\n{' '.join(['#' + h for h in seo['hashtags']])}"
            insta_id = post_reel(video_file, caption)
            log(f"Instagram posted! ID: {insta_id}")
        except Exception as insta_err:
            log(f"Instagram post failed (YouTube still uploaded): {insta_err}")

        mark_posted(topic)
        from agents.adaptive_scheduler import mark_upload_done
        mark_upload_done("english")
        log(f"SUCCESS: YouTube: {video_url}")

        clear_checkpoint(topic)

        from agents.cleanup_agent import cleanup_after_upload
        cleanup_after_upload(video_file, log_fn=log)

    except Exception as e:
        import traceback
        log(f"ERROR: {str(e)}")
        log(f"TRACEBACK: {traceback.format_exc()}")
    finally:
        db.release_lock("generation_english")


def track_views_job():
    has_secrets = os.path.exists("client_secrets.json") or os.environ.get("YOUTUBE_CLIENT_SECRETS_B64")
    if not has_secrets:
        print("[SKIP] No YouTube credentials found (file or env var) — view tracking disabled")
        return
    log("=== Tracking video view history ===")
    try:
        from agents.view_tracker_agent import track_views
        history = track_views()
        log(f"Tracked {len(history)} videos")

        try:
            from agents.close_ab_loop import close_loop
            close_loop(channel="english")
            log("Closed AB-title loop (actual_views_24h updated where due)")
        except Exception as ce:
            log(f"close_ab_loop skipped: {ce}")
    except Exception as e:
        import traceback
        log(f"ERROR (view tracking): {str(e)}")
        log(f"TRACEBACK: {traceback.format_exc()}")


def comment_reply_job():
    """Comment agent: draft replies for owner review (window/once-a-day gate and the
    enable/pause switches live in comment_engine). Never raises; never publishes unless the
    channel owner enabled publishing AND approved the draft."""
    try:
        from comment_engine.service import run_scheduled
        run_scheduled("english", log=log)
    except Exception as e:
        log(f"Comment agent job error: {e}")


def main():
    log("Scheduler run started (Cloud Run Job — single pass)")

    from agents.model_invoke_agent_english import reset_groq_budget
    reset_groq_budget()

    try:
        from agents.cleanup_agent import sweep_old_videos
        sweep_old_videos(max_age_hours=24, log_fn=log)
    except Exception as e:
        log(f"Cleanup skipped: {e}")

    # Always run view tracking + comment replies on every invocation
    track_views_job()
    comment_reply_job()
    # The comment job shares the Groq per-run call budget; give video
    # generation a fresh one so replies never push it onto the fallback.
    reset_groq_budget()

    # Manual override for testing: FORCE_GENERATE=true bypasses the
    # schedule gate and daily cap (replaces the old TEST_UPLOAD_ON_START
    # threading.Timer, which doesn't make sense in a one-shot Job).
    if os.environ.get("FORCE_GENERATE", "false").lower() == "true":
        log("FORCE_GENERATE=true — bypassing schedule gate and daily cap")
        run_generation_pipeline()
        return

    from agents.adaptive_scheduler import should_upload_now_for_channel
    should_run, reason = should_upload_now_for_channel("english")
    log(f"Schedule check: {reason}")
    if not should_run:
        log("Not a top-velocity hour — skipping generation this run")
        return

    # Daily cap by UTC calendar day (same definition the slot gate uses). A rolling
    # 24h window would block a slot at 17:00 because yesterday's 17:20 upload is
    # still inside it.
    _now = datetime.datetime.now(datetime.timezone.utc)
    posted_today = get_recent_topics(hours=_now.hour + _now.minute / 60 + _now.second / 3600)
    if len(posted_today) >= VIDEOS_PER_DAY:
        log(f"Daily cap reached ({len(posted_today)} posted today UTC) — skipping generation this run")
        return

    run_generation_pipeline()


if __name__ == "__main__":
    main()
