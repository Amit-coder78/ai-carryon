# scheduler_bhakti.py — single-run entrypoint for the Bhakti (devotional) channel
#
# Fifth pipeline, following the same single-pass-per-invocation pattern as
# scheduler.py / scheduler_hindi.py: one full generate+upload attempt per
# run, triggered hourly by Cloud Scheduler / Render cron / Railway cron.

import os
import key_fallback
key_fallback.install()  # per-channel key first, shared key on 429/401/403
import datetime

LOG_FILE = "output/scheduler_bhakti_log.txt"

# How many videos per day to aim for (fully adaptive)
VIDEOS_PER_DAY = int(os.environ.get("BHAKTI_VIDEOS_PER_DAY", "6"))

# "narrated" (default): Hindi voiceover + captions over a ducked bhajan track.
# "music_only": previous behaviour (no voice, no captions, bhajan only).
BHAKTI_MODE = os.environ.get("BHAKTI_MODE", "narrated").lower()


def log(message):
    os.makedirs("output", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    with open(LOG_FILE, "a") as f:
        f.write(entry + "\n")


def get_posted_today():
    from agents.database import db
    return db.get_recent_posted(hours=24, channel="bhakti")


def get_posted_count_today():
    return len(get_posted_today())


def mark_posted_today(topic):
    from agents.database import db
    try:
        db.mark_posted(topic, channel="bhakti")
    except Exception as e:
        log(f"DB mark_posted error: {e}")


def get_adaptive_hours():
    """Upload hours come ONLY from Bhakti velocity data in the database.
    Returns:
      list[int] - peak hours (UTC) backed by >= 3 samples each
      None      - no usable data yet (bootstrap: any hour is allowed)
      []        - DB lookup failed (skip this run rather than upload blindly)
    """
    MIN_SAMPLES = 3
    try:
        from agents_bhakti.velocity_agent import get_best_upload_windows_bhakti
        windows = get_best_upload_windows_bhakti(top_n=VIDEOS_PER_DAY)
        good = [w for w in windows if w["sample_count"] >= MIN_SAMPLES]
        if not good:
            log("No Bhakti peak-hour data yet — bootstrap mode (any hour allowed)")
            return None
        hours = sorted(w["hour"] for w in good[:VIDEOS_PER_DAY])
        log(f"Peak hours from DB (UTC): {hours}")
        return hours
    except Exception as e:
        log(f"Could not get peak hours from DB: {e}")
        return []


def should_generate_now():
    if get_posted_count_today() >= VIDEOS_PER_DAY:
        return False, "Daily quota reached"

    adaptive_hours = get_adaptive_hours()
    import datetime as _dt
    current_hour = _dt.datetime.utcnow().hour

    # None = bootstrap (no data yet), otherwise must be a DB peak hour
    if adaptive_hours is not None and current_hour not in adaptive_hours:
        return False, f"Current hour {current_hour:02d}:00 UTC not in DB peak hours {adaptive_hours}"

    try:
        from agents.database import db
        from datetime import datetime, timezone
        last = db.get_meta("last_upload_hour_bhakti")
        if last:
            gap = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
            if gap < 1.0:
                return False, f"Only {gap:.1f}h since last Bhakti upload — need 1h gap"
    except Exception:
        pass

    return True, f"Hour {current_hour:02d}:00 UTC OK (peak hours: {adaptive_hours or 'bootstrap'})"


def generate_and_upload_bhakti(force=False):
    from agents.database import db
    from agents.run_state import RunState

    state = RunState.open("bhakti", db.get_meta, db.set_meta, log=log)

    if force:
        log("Schedule check: BYPASSED (FORCE_GENERATE)")
    elif state.resumed:
        log("Schedule check: BYPASSED (finishing a run that stopped earlier)")
    else:
        should_run, reason = should_generate_now()
        log(f"Schedule check: {reason}")
        if not should_run:
            return

    acquired, age = db.try_acquire_lock("generation_bhakti", ttl_seconds=1800)
    if not acquired:
        log(f"Generation already in progress elsewhere (lock age {age:.0f}s) — skipping")
        return

    log("=== Bhakti video generation shuru hua ===")

    try:
        if state.resumed:
            topic = state.ctx["topic"]
            log(f"Resuming topic: {topic}")
        else:
            log("Bhakti topic dhundh raha hai...")
            from agents_bhakti.trending_agent import get_trending_topic
            topic = get_trending_topic(region_code="IN")
            log(f"Topic: {topic}")

            posted_today = get_posted_today()
            if topic in posted_today:
                log(f"Ye topic aaj already post ho chuka hai: {topic} — retrying trending_agent")
                topic = get_trending_topic(region_code="IN")
                log(f"Alternative topic: {topic}")
            state.begin({"topic": topic})

        def _saturation():
            from agents_bhakti.saturation_agent import check_saturation_bhakti
            s = check_saturation_bhakti(topic)
            log(f"Saturation: score={s['opportunity_score']} — {s['reason']}")
            return True

        def _comparison():
            from agents_bhakti.comparison_agent import compare_topic_bhakti
            c = compare_topic_bhakti(topic)
            ins = c.get("insights", {})
            if ins and not ins.get("error"):
                log(f"Comparison: avg views={ins.get('competitor_avg_views', 0):,}")
                return ins
            return {}

        if not state.has("saturation"):
            log("Saturation check ho raha hai...")
            try:
                state.step("saturation", _saturation)
            except Exception as se:
                log(f"Saturation check skip: {se}")

        log("Competitor comparison ho raha hai...")
        try:
            comparison_insights = state.step("comparison", _comparison)
        except Exception as ce:
            log(f"Comparison skip: {ce}")
            comparison_insights = {}

        def _research():
            log("Devotional research ho raha hai...")
            from agents_bhakti.research_agent import research
            return research(topic)

        research_data = state.step("research", _research)

        def _script():
            log("Bhakti script ban rahi hai...")
            from agents_bhakti.script_agent import create_script
            return create_script(research_data, topic=topic, comparison_insights=comparison_insights)

        script = state.step("script", _script)

        def _ab_title():
            from agents_bhakti.ab_title_agent import get_best_title_bhakti
            r = get_best_title_bhakti(topic, script)
            log(f"A/B winner: {r['winner']['title']} (score: {r['winner']['score']}/10)")
            return r["winner"]["title"]

        log("A/B title testing ho raha hai...")
        try:
            ab_winner_title = state.step("ab_title", _ab_title)
        except Exception as ae:
            log(f"A/B title test skip: {ae}")
            ab_winner_title = None

        def _seo():
            from agents_bhakti.seo_agent import generate_seo
            s = generate_seo(topic, script, comparison_insights=comparison_insights)
            if ab_winner_title:
                s["title"] = ab_winner_title
            return s

        log("Bhakti SEO generate ho raha hai...")
        seo = state.step("seo", _seo)
        log(f"Title: {seo['title']}")

        def _thumb():
            from agents_bhakti.thumbnail_agent import generate_thumbnail
            return generate_thumbnail(seo["title"], topic)

        log("Thumbnail ban raha hai...")
        thumbnail = state.step("thumbnail", _thumb, validate=os.path.exists)

        # Hook Engine: reverent, topic-relevant opening clip (bhakti profile only allows
        # emotional/mystery/curiosity hooks and blocks dramatic/violent/deity imagery).
        # Never blocks the run: no suitable hook -> hook=None -> renders as before.
        def _hook():
            from agents.hook_engine import select_hook
            from agents_bhakti.model_invoke_agent_bhakti import safe_invoke as _hook_invoke
            return select_hook(topic, script, "bhakti", invoke=_hook_invoke,
                               meta_get=db.get_meta, meta_set=db.set_meta)

        hook = None
        try:
            log("Hook clip select ho raha hai...")
            hook = state.step("hook", _hook,
                              validate=lambda h: h is None or os.path.exists(h.get("path", "")))
            log(f"Hook: {hook['clip_id']} ({hook['duration']}s, relevance {hook['relevance']:.2f})"
                if hook else "Hook: koi suitable nahi - normal opening")
        except Exception as he:
            log(f"Hook engine skip: {he}")

        def _images():
            log("Pexels devotional video clips fetch ho rahe hain...")
            from agents_bhakti.image_agent import generate_background_clips, generate_backgrounds
            paths, errors = generate_background_clips(topic, script, num_clips=4)
            if len(paths) < 2:
                log(f"Bahut kam Pexels clips mile ({errors}) — static images par fallback ho raha hai...")
                paths, errors = generate_backgrounds(topic, script, num_images=4)
                return [paths, False, errors]
            return [paths, True, errors]

        image_paths, use_pexels, errors = state.step(
            "images", _images,
            validate=lambda v: bool(v[0]) and all(os.path.exists(p) for p in v[0]))
        if not image_paths:
            log(f"Images nahi bani: {errors}")
            state.forget("images")
            return

        def _music():
            log("Bhakti background music dhoond rahe hain...")
            from agents_bhakti.music_agent import get_background_music
            m = get_background_music(topic, seo["title"])
            if not m:
                raise RuntimeError("No background music track found — aborting rather than uploading a silent video")
            return m

        music = state.step("music", _music, validate=lambda m: os.path.exists(m.get("path", "")))
        if music["license"] != "Creative Commons 0" and music["name"] not in seo["description"]:
            seo["description"] += (
                f"\n\nMusic: \"{music['name']}\" by {music['author']} "
                f"({music['license']}) - {music['url']}"
            )

        if BHAKTI_MODE == "music_only":
            def _video():
                log("Video ban raha hai (music-only, no narration)...")
                from agents_bhakti.silent_video_agent import create_silent_music_video
                return create_silent_music_video(music_path=music["path"],
                                                 hook=hook if use_pexels else None)
            video = state.step("video", _video, validate=os.path.exists, after=["images", "music"])
        else:
            def _voice():
                log("Hindi bhakti awaaz generate ho rahi hai...")
                from agents_bhakti.voice_agent import generate_voice
                return generate_voice(script)

            voice = state.step("voice", _voice, validate=os.path.exists, after=["images"])

            def _captions():
                log("Captions ban rahe hain...")
                from agents.caption_agent import create_srt
                create_srt(script, voice)
                return "output/captions.srt"

            state.step("captions", _captions, validate=os.path.exists, after=["voice"])

            def _video():
                log("Video ban raha hai (voiceover + bhajan background)...")
                if use_pexels:
                    from agents_bhakti.narrated_video_agent import create_narrated_music_video
                    return create_narrated_music_video(music_path=music["path"], audio_path=voice,
                                                      hook=hook)
                from agents.video_agent import create_video
                return create_video(use_pexels_clips=False, music_path=music["path"])

            video = state.step("video", _video, validate=os.path.exists,
                               after=["images", "voice", "captions", "music"])

        def _upload():
            log("YouTube Bhakti channel par upload ho raha hai...")
            from agents_bhakti.upload_agent import upload_video
            return list(upload_video(
                video_path=video,
                title=seo["title"],
                description=seo["description"],
                hashtags=seo["hashtags"],
                thumbnail_path=thumbnail
            ))

        # Never uploads twice: once this step is saved, a resumed run skips it.
        video_id, video_url = state.step("upload", _upload)

        if hook and use_pexels:
            from agents.hook_engine import record_usage as record_hook_usage
            record_hook_usage(hook, topic, "bhakti", db.get_meta, db.set_meta, video_id)

        mark_posted_today(topic)
        try:
            from agents.adaptive_scheduler import mark_upload_done
            mark_upload_done("bhakti")
        except Exception:
            pass
        log(f"SUCCESS: Upload ho gaya! {video_url}")
        state.clear()

        from agents.cleanup_agent import cleanup_after_upload
        cleanup_after_upload(video, log_fn=log)

    except Exception as e:
        import traceback
        log(f"ERROR: {str(e)}")
        log(f"TRACEBACK: {traceback.format_exc()}")
    finally:
        db.release_lock("generation_bhakti")


def track_views_bhakti_job():
    log("=== Bhakti view history track ho raha hai ===")
    try:
        from agents_bhakti.view_tracker_agent import track_views_bhakti
        history = track_views_bhakti()
        log(f"Tracked {len(history)} Bhakti videos")

        try:
            from agents.close_ab_loop import close_loop
            close_loop(channel="bhakti")
            log("Closed AB-title loop (actual_views_24h updated where due)")
        except Exception as ce:
            log(f"close_ab_loop skipped: {ce}")
    except Exception as e:
        import traceback
        log(f"ERROR (Bhakti view tracking): {str(e)}")
        log(f"TRACEBACK: {traceback.format_exc()}")


def comment_reply_job_bhakti():
    """Comment agent: draft replies for owner review (window/once-a-day gate and the
    enable/pause switches live in comment_engine). Never raises; never publishes unless the
    channel owner enabled publishing AND approved the draft."""
    try:
        from comment_engine.service import run_scheduled
        run_scheduled("bhakti", log=log)
    except Exception as e:
        log(f"Comment agent job error: {e}")


def main():
    log("Bhakti Scheduler run started (single pass, Fully Adaptive Mode)")

    from agents_bhakti.model_invoke_agent_bhakti import reset_groq_budget
    reset_groq_budget()

    try:
        from agents.cleanup_agent import sweep_old_videos
        sweep_old_videos(max_age_hours=24, log_fn=log)
    except Exception as e:
        log(f"Cleanup skipped: {e}")

    track_views_bhakti_job()
    comment_reply_job_bhakti()
    # Fresh Groq budget for generation (the comment job shares it).
    reset_groq_budget()

    if os.environ.get("FORCE_GENERATE", "false").lower() == "true":
        generate_and_upload_bhakti(force=True)
        return

    generate_and_upload_bhakti(force=False)


if __name__ == "__main__":
    main()
