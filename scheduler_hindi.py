# scheduler_hindi.py — single-run entrypoint for Cloud Run Jobs
#
# Same refactor as scheduler.py: no more infinite loop / schedule library /
# GitHub restore-backup dance. One pass per invocation, triggered hourly by
# Cloud Scheduler. See scheduler.py's module docstring for the full rationale.

import os
import key_fallback
key_fallback.install()  # per-channel key first, shared key on 429/401/403
import datetime

LOG_FILE = "output/scheduler_hindi_log.txt"

# How many videos per day to aim for (fully adaptive)
VIDEOS_PER_DAY = 10


def log(message):
    os.makedirs("output", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    with open(LOG_FILE, "a") as f:
        f.write(entry + "\n")


def get_posted_today():
    from agents.database import db
    return db.get_recent_posted(hours=24, channel="hindi")


def get_posted_count_today():
    return len(get_posted_today())


def mark_posted_today(topic):
    from agents.database import db
    try:
        db.mark_posted(topic, channel="hindi")
    except Exception as e:
        log(f"DB mark_posted error: {e}")


def get_adaptive_hours():
    """Top N best upload hours from real Hindi velocity data, falling back
    to spread-out defaults for any slots not yet backed by real data."""
    fallback = [2, 7, 13]  # UTC hours ≈ 7:30AM, 12:30PM, 6:30PM IST
    MIN_GOOD_WINDOWS = 3  # graduate as soon as we have this many real hours

    try:
        from agents_hindi.velocity_agent import get_best_upload_windows_hindi
        windows = get_best_upload_windows_hindi(top_n=VIDEOS_PER_DAY)
        good_windows = [w for w in windows if w["sample_count"] >= 3]

        if len(good_windows) >= MIN_GOOD_WINDOWS:
            hours = [w["hour"] for w in good_windows[:VIDEOS_PER_DAY]]
            for h in fallback:
                if len(hours) >= VIDEOS_PER_DAY:
                    break
                if h not in hours:
                    hours.append(h)
            hours = sorted(hours)
            log(f"Adaptive hours (real+fallback mix, UTC): {hours}")
            return hours
    except Exception as e:
        log(f"Could not get adaptive hours: {e}")

    log(f"Not enough Hindi data yet — using fallback hours (UTC): {fallback}")
    return fallback


def should_generate_now():
    if get_posted_count_today() >= VIDEOS_PER_DAY:
        return False, "Daily quota reached"

    adaptive_hours = get_adaptive_hours()
    import datetime as _dt
    current_hour = _dt.datetime.utcnow().hour

    if current_hour in adaptive_hours:
        # Check 1 hour minimum gap from last upload
        try:
            from agents.database import db
            from datetime import datetime, timezone
            last = db.get_meta("last_upload_hour_hindi")
            if last:
                gap = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                if gap < 1.0:
                    return False, f"Only {gap:.1f}h since last Hindi upload — need 1h gap"
        except Exception:
            pass
        return True, f"Current hour {current_hour:02d}:00 UTC is in best hours {adaptive_hours}"

    return False, f"Current hour {current_hour:02d}:00 UTC not in best hours {adaptive_hours}"


def generate_and_upload_hindi(force=False):
    from agents.database import db
    from agents.run_state import RunState

    state = RunState.open("hindi", db.get_meta, db.set_meta, log=log)

    if force:
        log("Schedule check: BYPASSED (FORCE_GENERATE)")
    elif state.resumed:
        log("Schedule check: BYPASSED (finishing a run that stopped earlier)")
    else:
        should_run, reason = should_generate_now()
        log(f"Schedule check: {reason}")
        if not should_run:
            return

    acquired, age = db.try_acquire_lock("generation_hindi", ttl_seconds=1800)
    if not acquired:
        log(f"Generation already in progress elsewhere (lock age {age:.0f}s) — skipping")
        return

    log("=== Hindi video generation shuru hua ===")

    try:
        from agents_hindi.categories import pick_category, classify_topic, looks_hindi, has_unsupported_script

        if state.resumed:
            topic, category = state.ctx["topic"], state.ctx["category"]
            log(f"Resuming topic [{category}]: {topic}")
        else:
            log("Hindi trending topic dhundh raha hai...")
            from agents_hindi.spy_agent import get_best_hindi_topic, get_hindi_trending_topics
            from agents_hindi.trending_agent import get_trending_topic

            # V2: channel only makes experiment videos in science / ai / tech / gadgets.
            category = pick_category(last_category=db.get_meta("last_category_hindi"))
            log(f"Category chosen: {category}")

            best = get_best_hindi_topic(category=category)

            if best:
                topic = best['topic']
                category = best.get('category', category)
                log(f"Spy agent se topic mila [{category}]: {topic} ({best['views']:,} views)")
            else:
                log("24 ghante mein koi video nahi mili, trending agent use kar raha hai...")
                topic = get_trending_topic(region_code="IN", category=category)
                category = classify_topic(topic) or category
                log(f"Trending topic [{category}]: {topic}")

            if has_unsupported_script(topic):
                log(f"Topic Hindi/English script mein nahi hai ({topic[:40]}...) — LLM se naya topic bana raha hai")
                from agents_hindi.trending_agent import _fallback_topic
                topic = _fallback_topic(category=category)
                category = classify_topic(topic) or category
                log(f"Naya topic [{category}]: {topic}")

            posted_today = get_posted_today()
            if topic in posted_today:
                log(f"Ye topic aaj already post ho chuka hai: {topic}")
                all_topics = get_hindi_trending_topics()
                for t in all_topics:
                    if t['topic'] not in posted_today:
                        topic = t['topic']
                        category = t.get('category', category)
                        log(f"Alternative topic [{category}]: {topic}")
                        break

            state.begin({"topic": topic, "category": category})

        def _saturation():
            from agents_hindi.saturation_agent import check_saturation_hindi
            s = check_saturation_hindi(topic)
            log(f"Saturation: score={s['opportunity_score']} — {s['reason']}")
            return True

        def _comparison():
            from agents_hindi.comparison_agent import compare_topic_hindi
            c = compare_topic_hindi(topic)
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

        from agents.research_agent import research
        research_data = state.step("research", lambda: research(topic))

        def _write_script():
            log("Hindi script ban rahi hai...")
            from agents_hindi.script_agent import create_script
            s = create_script(research_data, topic=topic, comparison_insights=comparison_insights)
            if not looks_hindi(s):
                log("Script Hindi nahi lag rahi — ek baar dobara try kar raha hai...")
                s = create_script(research_data, topic=topic, comparison_insights=comparison_insights)
            return s

        script = state.step("script", _write_script)
        if not looks_hindi(script):
            log("Script dobara bhi Hindi nahi — is run ko skip kar raha hai (kuch upload nahi hoga)")
            state.clear()          # a bad topic must not be resumed again and again
            return

        def _ab_title():
            from agents_hindi.ab_title_agent import get_best_title_hindi
            r = get_best_title_hindi(topic, script)
            log(f"A/B winner: {r['winner']['title']} (score: {r['winner']['score']}/10)")
            return r["winner"]["title"]

        log("A/B title testing ho raha hai...")
        try:
            ab_winner_title = state.step("ab_title", _ab_title)
        except Exception as ae:
            log(f"A/B title test skip: {ae}")
            ab_winner_title = None

        def _seo():
            from agents_hindi.seo_agent import generate_seo
            s = generate_seo(topic, script, comparison_insights=comparison_insights)
            if ab_winner_title:
                s["title"] = ab_winner_title
            return s

        log("Hindi SEO generate ho raha hai...")
        seo = state.step("seo", _seo)
        if not looks_hindi(f"{seo['title']} {seo.get('description', '')}"):
            log("Title/description Hindi nahi lag rahe — is run ko skip kar raha hai (kuch upload nahi hoga)")
            state.clear()
            return
        log(f"Title: {seo['title']}")

        def _thumb():
            from agents.thumbnail_generator import generate_thumbnail
            return generate_thumbnail(seo["title"], topic)

        log("Thumbnail ban raha hai...")
        thumbnail = state.step("thumbnail", _thumb, validate=os.path.exists)

        # Hook Engine: topic-relevant opening clip, chosen BEFORE the normal clips.
        # Never blocks the run: no suitable hook -> hook=None -> renders as before.
        def _hook():
            from agents.hook_engine import select_hook
            from agents_hindi.model_invoke_agent_hindi import safe_invoke as _hook_invoke
            return select_hook(topic, script, "hindi", invoke=_hook_invoke,
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
            log("Pexels video clips fetch ho rahe hain...")
            from agents_hindi.video_clip_agent import generate_background_clips
            from agents.image_agent import generate_backgrounds
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

        def _voice():
            log("Hindi awaaz generate ho rahi hai...")
            from agents_hindi.voice_agent import generate_voice
            return generate_voice(script)

        voice = state.step("voice", _voice, validate=os.path.exists, after=["images"])

        def _captions():
            log("Captions ban rahe hain...")
            from agents.caption_agent import create_srt
            create_srt(script, voice)
            return "output/captions.srt"

        state.step("captions", _captions, validate=os.path.exists, after=["voice"])

        def _video():
            log("Video ban raha hai...")
            from agents.video_agent import create_video
            return create_video(use_pexels_clips=use_pexels, hook=hook if use_pexels else None)

        video = state.step("video", _video, validate=os.path.exists, after=["images", "voice", "captions"])

        def _upload():
            log("YouTube Hindi channel par upload ho raha hai...")
            from agents_hindi.upload_agent import upload_video
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
            record_hook_usage(hook, topic, "hindi", db.get_meta, db.set_meta, video_id)

        mark_posted_today(topic)
        try:
            db.set_meta(f"hindi_video_category:{video_id}", category)
            db.set_meta("last_category_hindi", category)
        except Exception as me:
            log(f"Category save skipped: {me}")
        try:
            from agents.adaptive_scheduler import mark_upload_done
            mark_upload_done("hindi")
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
        db.release_lock("generation_hindi")


def track_views_hindi_job():
    log("=== Hindi view history track ho raha hai ===")
    try:
        from agents_hindi.view_tracker_agent import track_views_hindi
        history = track_views_hindi()
        log(f"Tracked {len(history)} Hindi videos")

        try:
            from agents.close_ab_loop import close_loop
            close_loop(channel="hindi")
            log("Closed AB-title loop (actual_views_24h updated where due)")
        except Exception as ce:
            log(f"close_ab_loop skipped: {ce}")
    except Exception as e:
        import traceback
        log(f"ERROR (Hindi view tracking): {str(e)}")
        log(f"TRACEBACK: {traceback.format_exc()}")


def comment_reply_job_hindi():
    """Comment agent: draft replies for owner review (window/once-a-day gate and the
    enable/pause switches live in comment_engine). Never raises; never publishes unless the
    channel owner enabled publishing AND approved the draft."""
    try:
        from comment_engine.service import run_scheduled
        run_scheduled("hindi", log=log)
    except Exception as e:
        log(f"Comment agent job error: {e}")


def main():
    log("Hindi Scheduler run started (Cloud Run Job — single pass, Fully Adaptive Mode)")

    from agents_hindi.model_invoke_agent_hindi import reset_groq_budget
    reset_groq_budget()

    try:
        from agents.cleanup_agent import sweep_old_videos
        sweep_old_videos(max_age_hours=24, log_fn=log)
    except Exception as e:
        log(f"Cleanup skipped: {e}")

    track_views_hindi_job()
    comment_reply_job_hindi()
    # Fresh Groq budget for generation (the comment job shares it).
    reset_groq_budget()

    if os.environ.get("FORCE_GENERATE", "false").lower() == "true":
        generate_and_upload_hindi(force=True)
        return

    generate_and_upload_hindi(force=False)


if __name__ == "__main__":
    main()
