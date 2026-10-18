"""
agents/adaptive_scheduler.py — Phase 4: Adaptive Upload Scheduling

Analyzes real view velocity data from SQLite to find the best hour
to upload. Replaces the fixed 5-hour interval with intelligence.

Usage:
    from agents.adaptive_scheduler import get_optimal_upload_time, should_upload_now
"""

import os
import json
from datetime import datetime, timezone, timedelta


def get_peak_hours_analysis():
    """
    Get peak hours from SQLite DB.
    Falls back to velocity_agent JSON method if DB not ready.
    """
    try:
        from agents.database import db
        peak_hours = db.get_peak_hours()
        total_samples = sum(h["sample_count"] for h in peak_hours.values())
        if total_samples >= 10:
            return peak_hours, "db"
    except Exception as e:
        print(f"DB peak hours error: {e}")

    # Fallback to velocity_agent JSON method
    try:
        from agents.velocity_agent import load_and_analyse
        analysis = load_and_analyse("output/view_history.json")
        peak_hours = analysis.get("peak_hours", {})
        return peak_hours, "json"
    except Exception as e:
        print(f"Velocity agent error: {e}")

    return {}, "none"


def get_best_upload_windows(top_n=5):
    """Return top N upload hours by avg velocity."""
    peak_hours, source = get_peak_hours_analysis()
    if not peak_hours:
        return [], source

    windows = [
        {
            "hour": int(h),
            "avg_velocity": data["avg_velocity"],
            "sample_count": data["sample_count"],
        }
        for h, data in peak_hours.items()
        if data["sample_count"] >= 2
    ]

    windows.sort(key=lambda x: x["avg_velocity"], reverse=True)
    return windows[:top_n], source


def get_optimal_upload_hour():
    """
    Return the single best UTC hour to upload.
    Returns None if not enough data (< 3 samples in best window).
    """
    try:
        from agents.database import db
        hour = db.get_best_upload_hour()
        if hour is not None:
            return hour
    except Exception:
        pass

    # Fallback
    windows, _ = get_best_upload_windows(top_n=1)
    if windows and windows[0]["sample_count"] >= 3:
        return windows[0]["hour"]
    return None


def should_upload_now(tolerance_minutes=30):
    """
    Check if current time is within tolerance_minutes of the optimal hour.
    Returns (bool, reason_string).
    """
    best_hour = get_optimal_upload_hour()
    now_utc = datetime.now(timezone.utc)

    if best_hour is None:
        return True, "Not enough data yet — uploading immediately"

    current_hour = now_utc.hour
    current_minute = now_utc.minute

    # Check if we're within tolerance of the best hour
    minutes_into_hour = current_minute
    minutes_until_best = ((best_hour - current_hour) % 24) * 60 - minutes_into_hour

    if minutes_until_best <= 0 or minutes_until_best <= tolerance_minutes:
        return True, f"At or near best upload hour {best_hour:02d}:00 UTC"

    if minutes_until_best > 60 * 6:
        # More than 6 hours away — don't wait, upload now
        return True, f"Best hour {best_hour:02d}:00 UTC is too far — uploading now"

    return False, f"Wait {minutes_until_best} min for best hour {best_hour:02d}:00 UTC"


def wait_for_optimal_time(max_wait_minutes=90, log_fn=None):
    """
    Wait until optimal upload time if it's within max_wait_minutes.
    Calls log_fn(msg) for progress updates.
    """
    import time

    if log_fn is None:
        log_fn = print

    best_hour = get_optimal_upload_hour()
    if best_hour is None:
        log_fn("Adaptive scheduler: no peak hour data yet, uploading immediately")
        return

    now_utc = datetime.now(timezone.utc)
    target = now_utc.replace(hour=best_hour, minute=0, second=0, microsecond=0)

    if target < now_utc:
        target += timedelta(days=1)

    wait_seconds = (target - now_utc).total_seconds()

    if wait_seconds <= 0 or wait_seconds > max_wait_minutes * 60:
        log_fn(
            f"Adaptive scheduler: best hour is {best_hour:02d}:00 UTC "
            f"({wait_seconds/60:.0f} min away) — uploading now"
        )
        return

    log_fn(
        f"Adaptive scheduler: waiting {wait_seconds/60:.0f} min "
        f"for best upload window {best_hour:02d}:00 UTC"
    )
    time.sleep(wait_seconds)
    log_fn(f"Adaptive scheduler: reached {best_hour:02d}:00 UTC — uploading now")


def get_schedule_recommendation():
    """
    Return a human-readable scheduling recommendation.
    Used by dashboard.
    """
    windows, source = get_best_upload_windows(top_n=3)
    best_hour = get_optimal_upload_hour()

    if not windows:
        return {
            "status": "insufficient_data",
            "message": "Need more view snapshots to detect peak hours. Keep running hourly tracking.",
            "best_hour": None,
            "windows": [],
            "source": source,
        }

    top = windows[0]
    ist_hour = (top["hour"] + 5) % 24
    ist_min = 30 if top["hour"] + 5 >= 24 or (top["hour"] + 5) % 24 != (top["hour"] + 5 + 0.5) % 24 else 0
    ist_label = f"{ist_hour:02d}:30 IST" if True else f"{ist_hour:02d}:00 IST"

    return {
        "status": "ready",
        "message": (
            f"Best upload window: {top['hour']:02d}:00 UTC (~{ist_hour:02d}:30 IST). "
            f"Avg velocity: {top['avg_velocity']:.1f} views/hr from {top['sample_count']} samples."
        ),
        "best_hour": best_hour,
        "windows": windows,
        "source": source,
    }



# ─────────────────────────────────────────────────────────────────────────────
# Blended upload scoring: audience prior + learned velocity + upload outcomes
#
# Three signals, from weakest to strongest:
#   1. PRIOR    - the YouTube Studio "When your viewers are on YouTube" heatmap.
#                 It is a starting guess only (YouTube has no API for it).
#   2. VELOCITY - views gained per hour from our own hourly snapshots (when views
#                 ARRIVE). Both 1 and 2 describe viewer activity, so they are
#                 shifted by UPLOAD_LEAD_HOURS to get the best hour to UPLOAD.
#   3. OUTCOME  - what actually happened: views 24h after publish, grouped by the
#                 UTC hour the video was uploaded. This is the direct signal.
#
#   audience(h) = (PRIOR_STRENGTH*prior(h) + n_v(h)*velocity(h)) / (PRIOR_STRENGTH + n_v(h))
#   score(h)    = (OUTCOME_STRENGTH*upload_shift(audience)(h) + n_o(h)*outcome(h))
#                 / (OUTCOME_STRENGTH + n_o(h))
#
# Few samples -> the prior leads. More samples -> our own data takes over.
# Only the last LEARN_WINDOW_DAYS days are used, so it follows audience changes.
#
# Update the prior WITHOUT a code change: store a JSON list of 24 numbers
# (0..1, index = IST hour) in the DB meta key  audience_prior_ist_<channel>:
#   db.set_meta("audience_prior_ist_english", json.dumps([...24 numbers...]))
# Channels with no prior (and no meta key) keep the old behaviour.
# ─────────────────────────────────────────────────────────────────────────────

LEARN_WINDOW_DAYS = int(os.environ.get("SCHEDULER_LEARN_WINDOW_DAYS", "28"))
PRIOR_STRENGTH = float(os.environ.get("SCHEDULER_PRIOR_STRENGTH", "6"))     # prior "worth" N velocity samples
OUTCOME_STRENGTH = float(os.environ.get("SCHEDULER_OUTCOME_STRENGTH", "3")) # ...and N videos for outcomes
OUTCOME_MIN_VIDEOS = int(os.environ.get("SCHEDULER_OUTCOME_MIN_VIDEOS", "3"))  # per upload hour
ALLOWED_TOP_HOURS = int(os.environ.get("SCHEDULER_ALLOWED_TOP_HOURS", "6"))
UPLOAD_LEAD_HOURS = (1, 2)   # video should be live 1-2h before viewers peak

# No built-in audience heatmap: English now learns its hours only from its own
# data (see _learned_slot_decision). A prior can still be supplied through the DB
# meta key audience_prior_ist_<channel> if a channel wants one.
DEFAULT_AUDIENCE_PRIOR_IST = {}


def _ist_curve_to_utc(ist_curve):
    """IST = UTC+5:30, so UTC hour h covers IST h+5:30..h+6:30 -> average two IST hours."""
    return [(ist_curve[(h + 5) % 24] + ist_curve[(h + 6) % 24]) / 2 for h in range(24)]


def _load_audience_prior_utc(channel, db=None):
    """Return 24 weights (index = UTC hour), or None if this channel has no prior."""
    curve = None
    if db is not None:
        try:
            raw = db.get_meta(f"audience_prior_ist_{channel}")
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and len(parsed) == 24:
                    curve = [float(x) for x in parsed]
        except Exception as e:
            print(f"[{channel}] bad audience prior in DB meta, using default: {e}")
    if curve is None:
        curve = DEFAULT_AUDIENCE_PRIOR_IST.get(channel)
    if curve is None:
        return None
    top = max(curve) or 1.0
    return _ist_curve_to_utc([c / top for c in curve])


def _collect_recent_signals(db, channel, now):
    """
    One pass over the stored snapshots (last LEARN_WINDOW_DAYS days only).
    Returns:
      velocity (24 floats 0..1), velocity_n (24 ints)   - by hour views ARRIVED
      outcome  (24 floats 0..1), outcome_n  (24 ints)   - by hour video was UPLOADED
    """
    from agents.database import _parse_ts_safe
    cutoff = now - timedelta(days=LEARN_WINDOW_DAYS)

    vsum, vcnt = [0.0] * 24, [0] * 24
    uploads = []   # (upload_hour_utc, views_at_24h)

    for data in db.get_all_snapshots(channel=channel).values():
        snaps = data.get("snapshots") or []

        # velocity: views per hour, keyed by the hour of the later snapshot
        for i in range(1, len(snaps)):
            try:
                t1 = _parse_ts_safe(snaps[i - 1]["timestamp"])
                t2 = _parse_ts_safe(snaps[i]["timestamp"])
                if t2 < cutoff:
                    continue
                hrs = (t2 - t1).total_seconds() / 3600
                if hrs <= 0:
                    continue
                gained = max(snaps[i]["views"] - snaps[i - 1]["views"], 0)
                vsum[t2.hour] += gained / hrs
                vcnt[t2.hour] += 1
            except Exception:
                continue

        # outcome: views ~24h after publish, grouped by upload hour
        try:
            pub_raw = data.get("published")
            if not pub_raw or not snaps:
                continue
            pub = _parse_ts_safe(pub_raw)
            if pub < cutoff or now - pub < timedelta(hours=24):
                continue
            target = pub + timedelta(hours=24)
            best, best_gap = None, None
            for sn in snaps:
                gap = abs((_parse_ts_safe(sn["timestamp"]) - target).total_seconds())
                if best_gap is None or gap < best_gap:
                    best, best_gap = sn, gap
            if best is not None and best_gap <= 6 * 3600:   # snapshot within 6h of the 24h mark
                uploads.append((pub.hour, float(best["views"])))
        except Exception:
            continue

    vavg = [vsum[h] / vcnt[h] if vcnt[h] else 0.0 for h in range(24)]
    vtop = max(vavg) or 1.0
    velocity = [a / vtop for a in vavg]

    # outcome: views_24h relative to the channel's median, averaged per upload hour
    outcome, ocnt = [0.0] * 24, [0] * 24
    if uploads:
        ordered = sorted(v for _, v in uploads)
        median = ordered[len(ordered) // 2] or 1.0
        osum = [0.0] * 24
        for hour, views in uploads:
            osum[hour] += min(views / median, 5.0)     # cap so one viral video can't dominate
            ocnt[hour] += 1
        oavg = [osum[h] / ocnt[h] if ocnt[h] >= OUTCOME_MIN_VIDEOS else 0.0 for h in range(24)]
        otop = max(oavg) or 1.0
        outcome = [a / otop for a in oavg]
        ocnt = [c if c >= OUTCOME_MIN_VIDEOS else 0 for c in ocnt]
    return velocity, vcnt, outcome, ocnt


def compute_upload_scores(channel, now=None, db=None):
    """
    Score every UTC hour as an UPLOAD hour (0..1, higher = better).
    Returns (scores, info), or (None, None) if the channel has no prior.
    """
    now = now or datetime.now(timezone.utc)
    if db is None:
        from agents.database import db as _db
        db = _db
    prior = _load_audience_prior_utc(channel, db)
    if prior is None:
        return None, None
    try:
        velocity, vn, outcome, on = _collect_recent_signals(db, channel, now)
    except Exception as e:
        print(f"[{channel}] could not read snapshots, using prior only: {e}")
        velocity, vn, outcome, on = [0.0] * 24, [0] * 24, [0.0] * 24, [0] * 24

    audience = [
        (PRIOR_STRENGTH * prior[h] + vn[h] * velocity[h]) / (PRIOR_STRENGTH + vn[h])
        for h in range(24)
    ]
    shifted = [
        sum(audience[(h + lead) % 24] for lead in UPLOAD_LEAD_HOURS) / len(UPLOAD_LEAD_HOURS)
        for h in range(24)
    ]
    scores = [
        (OUTCOME_STRENGTH * shifted[h] + on[h] * outcome[h]) / (OUTCOME_STRENGTH + on[h])
        for h in range(24)
    ]
    return scores, {"velocity_samples": sum(vn), "outcome_videos": sum(on), "outcome_n": on}


def _blended_schedule_decision(channel, now):
    """(bool, reason) from blended scores, or None if the channel has no prior."""
    try:
        from agents.database import db
        scores, info = compute_upload_scores(channel, now=now, db=db)
    except Exception as e:
        print(f"[{channel}] blended scheduler error, using old logic: {e}")
        return None
    if scores is None:
        return None

    ranked = sorted(range(24), key=lambda h: scores[h], reverse=True)
    allowed = sorted(ranked[:ALLOWED_TOP_HOURS])
    hour = now.hour
    detail = (f"{info['velocity_samples']} velocity samples, "
              f"{info['outcome_videos']} scored videos")
    if hour not in allowed:
        return False, (
            f"[{channel}] Hour {hour:02d}:00 UTC (score {scores[hour]:.2f}) not in best "
            f"{ALLOWED_TOP_HOURS} upload hours {allowed} ({detail})"
        )
    try:
        last_iso = db.get_meta(f"last_upload_hour_{channel}")
        if last_iso:
            gap = (now - datetime.fromisoformat(last_iso)).total_seconds() / 3600
            if gap < 1.0:
                return False, f"[{channel}] Only {gap:.1f}h since last upload — need 1h gap"
    except Exception:
        pass
    return True, (
        f"[{channel}] Hour {hour:02d}:00 UTC (score {scores[hour]:.2f}) is in best "
        f"{ALLOWED_TOP_HOURS} upload hours {allowed} ({detail})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Learned daily slots (no hard-coded hours, no default hour list)
#
# Each hourly run: score every UTC hour from OWN velocity + 24h-outcome data,
# take the top DAILY_SLOTS hours (kept >= SLOT_MIN_SPACING_H apart), and treat
# them as today's upload slots. A slot is "due" once its hour has arrived, and
# we generate whenever due slots > uploads already made today (UTC day), so a
# slot missed by a late/failed run is caught up on the next run instead of lost.
# ─────────────────────────────────────────────────────────────────────────────

DAILY_SLOTS = int(os.environ.get("SCHEDULER_DAILY_SLOTS", "3"))
SLOT_MIN_SPACING_H = int(os.environ.get("SCHEDULER_SLOT_MIN_SPACING_H", "3"))
LEARN_MIN_SAMPLES = int(os.environ.get("SCHEDULER_LEARN_MIN_SAMPLES", "10"))
COLD_START_GAP_H = float(os.environ.get("SCHEDULER_COLD_START_GAP_H", "8"))
LEARNED_SLOT_CHANNELS = {
    c.strip() for c in os.environ.get("SCHEDULER_LEARNED_SLOT_CHANNELS", "english").split(",") if c.strip()
}


def _pick_slots(scores, n, min_spacing_h):
    """Top-n hours by score, at least min_spacing_h apart (circular). Sorted."""
    chosen = []
    for h in sorted(range(24), key=lambda x: scores[x], reverse=True):
        if all(min((h - c) % 24, (c - h) % 24) >= min_spacing_h for c in chosen):
            chosen.append(h)
        if len(chosen) == n:
            break
    return sorted(chosen)


def _learned_slot_decision(channel, now):
    """(bool, reason). Never returns None; falls back to spacing, not fixed hours."""
    from agents.database import db

    done = len(db.get_recent_posted(
        hours=now.hour + now.minute / 60 + now.second / 3600, channel=channel))
    if done >= DAILY_SLOTS:
        return False, f"[{channel}] Daily target reached ({done}/{DAILY_SLOTS} today UTC)"

    last_dt = None
    try:
        last_iso = db.get_meta(f"last_upload_hour_{channel}")
        if last_iso:
            last_dt = datetime.fromisoformat(last_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            gap = (now - last_dt).total_seconds() / 3600
            if gap < 1.0:
                return False, f"[{channel}] Only {gap:.1f}h since last upload - need 1h gap"
    except Exception:
        last_dt = None

    try:
        velocity, vn, outcome, on = _collect_recent_signals(db, channel, now)
    except Exception as e:
        print(f"[{channel}] could not read snapshots: {e}")
        vn = [0] * 24

    if sum(vn) < LEARN_MIN_SAMPLES:
        # Cold start: no data yet, so just space uploads out. No fixed hours.
        if last_dt is None:
            return True, f"[{channel}] Cold start: no data and no previous upload"
        gap = (now - last_dt).total_seconds() / 3600
        if gap >= COLD_START_GAP_H:
            return True, f"[{channel}] Cold start: {gap:.1f}h since last upload"
        return False, f"[{channel}] Cold start: waiting ({gap:.1f}h < {COLD_START_GAP_H}h)"

    # velocity says when views ARRIVE; shift back so we score the UPLOAD hour
    shifted = [
        sum(velocity[(h + lead) % 24] for lead in UPLOAD_LEAD_HOURS) / len(UPLOAD_LEAD_HOURS)
        for h in range(24)
    ]
    scores = [
        (OUTCOME_STRENGTH * shifted[h] + on[h] * outcome[h]) / (OUTCOME_STRENGTH + on[h])
        for h in range(24)
    ]
    slots = _pick_slots(scores, DAILY_SLOTS, SLOT_MIN_SPACING_H)
    due = sum(1 for h in slots if h <= now.hour)
    detail = f"slots={slots}, due={due}, done={done}, {sum(vn)} velocity samples"
    if due > done:
        return True, f"[{channel}] Hour {now.hour:02d}:00 UTC - slot due ({detail})"
    return False, f"[{channel}] Hour {now.hour:02d}:00 UTC - waiting for next slot ({detail})"



def should_upload_now_for_channel(channel="english", top_n=5, tolerance_hours=1):
    """
    Multi-channel adaptive scheduler.
    Returns (bool, reason_string).

    Logic:
    - Get top `top_n` peak hours for the channel (priority queue by avg velocity)
    - Current hour must be within `tolerance_hours` of one of those peak hours
      (not just an exact match — since Cloud Scheduler only fires on the hour,
      an exact match can miss a peak window that shifts by a bit as new data
      comes in)
    - Minimum 1 hour gap between consecutive uploads (checked via last_upload_hour in DB)
    - Falls back to default hours if not enough data
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    current_hour = now.hour

    if channel in LEARNED_SLOT_CHANNELS:
        return _learned_slot_decision(channel, now)

    # Prior (audience heatmap) + learned velocity + upload outcomes. Only for
    # channels that have a prior; the rest fall through to the original logic.
    decision = _blended_schedule_decision(channel, now)
    if decision is not None:
        return decision

    # Default fallback hours per channel
    defaults = {
        "english": [6, 12, 18],   # 6am, 12pm, 6pm UTC
        "hindi":   [3, 9, 15],    # IST 8:30am, 2:30pm, 8:30pm
        "cricket": [4, 10, 16],   # IST 9:30am, 3:30pm, 9:30pm
    }

    try:
        from agents.database import db
        peak_hours = db.get_peak_hours(channel=channel)
        total_samples = sum(h["sample_count"] for h in peak_hours.values()) if peak_hours else 0

        if total_samples >= 10:
            # Build priority queue — top N hours by avg velocity
            windows = sorted(
                [{"hour": int(h), "avg_velocity": d["avg_velocity"], "samples": d["sample_count"]}
                 for h, d in peak_hours.items() if d["sample_count"] >= 2],
                key=lambda x: x["avg_velocity"], reverse=True
            )[:top_n]
            top_hours = [w["hour"] for w in windows]

            # Match if current hour is within tolerance_hours of ANY peak hour
            # (circular distance, so e.g. hour 23 is 1 away from hour 0)
            def _circular_distance(a, b):
                d = abs(a - b)
                return min(d, 24 - d)

            matched_hour = next(
                (h for h in top_hours if _circular_distance(current_hour, h) <= tolerance_hours),
                None
            )

            if matched_hour is None:
                return False, (
                    f"[{channel}] Hour {current_hour:02d}:00 UTC not within "
                    f"{tolerance_hours}h of top-{top_n} peak hours {top_hours}"
                )

            # Check minimum 1 hour gap from last upload
            try:
                last_upload_iso = db.get_meta(f"last_upload_hour_{channel}")
                if last_upload_iso:
                    from datetime import datetime
                    last_dt = datetime.fromisoformat(last_upload_iso)
                    gap_hours = (now - last_dt).total_seconds() / 3600
                    if gap_hours < 1.0:
                        return False, f"[{channel}] Only {gap_hours:.1f}h since last upload — need 1h gap"
            except Exception:
                pass

            return True, (
                f"[{channel}] Hour {current_hour:02d}:00 UTC is within {tolerance_hours}h of "
                f"peak hour {matched_hour:02d}:00 (top-{top_n}: {top_hours})"
            )

    except Exception as e:
        print(f"[{channel}] DB peak hours error: {e}")

    # Fallback to default hours
    fallback = defaults.get(channel, [6, 12, 18])
    if current_hour in fallback:
        return True, f"[{channel}] Hour {current_hour:02d}:00 UTC matches fallback schedule {fallback}"
    return False, f"[{channel}] Hour {current_hour:02d}:00 UTC not in fallback schedule {fallback}"


def mark_upload_done(channel="english"):
    """Call this after a successful upload to enforce the 1-hour gap."""
    try:
        from datetime import datetime, timezone
        from agents.database import db
        db.set_meta(f"last_upload_hour_{channel}", datetime.now(timezone.utc).isoformat())
    except Exception as e:
        print(f"mark_upload_done failed: {e}")

if __name__ == "__main__":
    print("Adaptive Scheduler Analysis")
    print("=" * 40)
    rec = get_schedule_recommendation()
    print(f"Status: {rec['status']}")
    print(f"Message: {rec['message']}")
    print()
    scores, info = compute_upload_scores("english")
    if scores is not None:
        print()
        print(f"Blended upload scores, English ({info['velocity_samples']} velocity samples, "
              f"{info['outcome_videos']} scored videos):")
        for h in sorted(range(24), key=lambda x: scores[x], reverse=True)[:ALLOWED_TOP_HOURS]:
            print(f"  {h:02d}:00 UTC (~{(h + 5) % 24:02d}:30 IST) — score {scores[h]:.2f}")
    print()
    print("Top upload windows:")
    for w in rec["windows"]:
        ist = (w["hour"] + 5) % 24
        print(f"  {w['hour']:02d}:00 UTC (~{ist:02d}:30 IST) — {w['avg_velocity']:.1f} views/hr ({w['sample_count']} samples)")
