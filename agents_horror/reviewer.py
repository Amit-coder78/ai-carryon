# agents_horror/reviewer.py
"""Stage 10: horror quality reviewer. Deterministic checks (no extra LLM call, no quota).
Every issue carries a `fix` hint so the scheduler repairs only the broken part:
  resource_scene -> re-search that scene's clip       rewrite_script -> one length rewrite
  none           -> report only (a human looks at it)
A full re-render is never triggered by a minor issue."""
import os

from agents_horror.config import FICTION_LABEL
from agents_horror.schemas import validate_scenes
from agents_horror.series import similarity
from agents_horror.story_agent import split_sentences, STOCK_OPENERS, REAL_CLAIMS

ERROR, WARN = "error", "warning"


def _issue(code, severity, message, scene_id=None, fix="none"):
    return {"code": code, "severity": severity, "message": message, "scene_id": scene_id, "fix": fix}


def review(cfg, script, bible, scenes, assets=None, voice=None, sound=None, video=None, metadata=None,
           recent_premises=(), premise="", get_duration=None):
    """Any of assets/voice/sound/video/metadata may be None (pre-render review)."""
    issues = []
    add = lambda *a, **k: issues.append(_issue(*a, **k))
    sentences = split_sentences(script)

    # 1. hook
    if sentences:
        if len(sentences[0].split()) > 18:
            add("hook_long", WARN, "hook is over 18 words; the first 1-3 seconds will not land")
        if sentences[0].lower().startswith(STOCK_OPENERS):
            add("hook_cliche", WARN, "hook opens with a stock phrase")
    else:
        add("empty_script", ERROR, "script has no sentences", fix="rewrite_script")

    # 2. originality / coherence
    if sentences and len({s.lower() for s in sentences}) < 0.9 * len(sentences):
        add("repeated_sentences", WARN, "script repeats sentences")
    if premise and any(similarity(premise, p) >= 0.6 for p in recent_premises):
        add("repeats_recent_premise", WARN, "premise is very close to a recent video")
    if sentences:
        if len(sentences[-1].split()) < 4:
            add("weak_ending", WARN, "ending line is very short; check it is a real payoff or cliffhanger")
        tail = " ".join(sentences[-max(2, len(sentences) // 3):]).lower()
        anchors = [x.name.lower().split()[0] for x in (*bible.characters[:1], *bible.entities[:1], *bible.locations[:1]) if x.name]
        if anchors and not any(a in tail for a in anchors):
            add("ending_detached", WARN, "ending never mentions the main character, entity or place from the setup")

    # 3. scene plan: order, timing, bible references
    for e in validate_scenes(scenes, bible, total_sec=None):
        add("scene_plan", ERROR, e)
    used = {c for sc in scenes for c in (*sc.characters, *sc.bible_refs)}
    for ch in bible.characters:
        if ch.id not in used:
            add("bible_unused", WARN, f"character '{ch.id}' is in the bible but in no scene")

    # 4. visuals vs narration
    if assets is not None:
        for sc in scenes:
            a = assets.get(sc.scene_id) or {}
            st = a.get("status", "missing")
            if st == "missing":
                add("scene_clip_missing", ERROR, f"no relevant clip for {sc.scene_id}; flagged for review",
                    scene_id=sc.scene_id, fix="resource_scene")
            elif st == "reused_same_location":
                add("scene_clip_reused", WARN, a.get("note", "clip reused from same location"), scene_id=sc.scene_id)
            elif a.get("relevance", 1.0) < cfg.min_relevance:
                add("scene_clip_weak", WARN, f"clip for {sc.scene_id} is only weakly relevant", scene_id=sc.scene_id,
                    fix="resource_scene")
            if st in ("ok", "reused_same_location") and not (a.get("path") and os.path.exists(a["path"])):
                add("scene_clip_invalid", ERROR, f"clip file for {sc.scene_id} is missing on disk",
                    scene_id=sc.scene_id, fix="resource_scene")

    # 5. narration length
    if voice:
        total = voice["total"]
        if not cfg.is_long and not 45 <= total <= 60:
            add("narration_duration", ERROR, f"narration is {total:.1f}s; Shorts need 45-60s", fix="rewrite_script")
        elif cfg.is_long and abs(total - cfg.duration_sec) > cfg.duration_sec * 0.25:
            add("narration_duration", WARN, f"narration is {total:.0f}s, target {cfg.duration_sec}s")

    # 6. sound design timing
    if sound is not None:
        sfx = sorted(sound.get("sfx", []), key=lambda c: c["at"])
        if len(sfx) > cfg.max_sfx:
            add("sfx_overload", WARN, "too many sound effects")
        for prev, cur in zip(sfx, sfx[1:]):
            if cur["at"] - prev["at"] < 5.0:
                add("sfx_crowded", WARN, f"effects at {prev['at']}s and {cur['at']}s are too close")
        if any(c["at"] < 2.0 for c in sfx):
            add("sfx_early", WARN, "an effect fires in the first 2 seconds, over the hook")
        if cfg.music and not sound.get("bed"):
            add("no_ambience", WARN, "no ambience bed (FREESOUND_API_KEY missing or no match)")

    # 7. metadata / fiction label
    if metadata is not None:
        # the fiction label itself says "Not a real incident": remove it before looking for false real-claims
        description = metadata.get("description", "").replace(FICTION_LABEL, "")
        blob = " ".join([metadata.get("title", ""), description, script]).lower()
        if "fiction" not in metadata.get("description", "").lower():
            add("no_fiction_label", ERROR, "description does not label the story as fiction")
        for phrase in REAL_CLAIMS:
            if phrase in blob:
                add("claims_real", ERROR, f"text presents fiction as real ('{phrase}')")

    # 8. final video
    if video is not None:
        p = video.get("path", "")
        if not (p.endswith(".mp4") and os.path.exists(p) and os.path.getsize(p) > 0):
            add("video_invalid", ERROR, "final video is missing or empty")
        else:
            dur = video.get("probed_duration")
            if dur is None and get_duration:
                try:
                    dur = get_duration(p)
                except Exception as e:
                    add("video_probe_failed", WARN, f"could not read video duration: {e}")
            if dur is not None:
                if voice and abs(dur - voice["total"]) > 0.8:
                    add("av_mismatch", ERROR, f"video {dur:.1f}s vs narration {voice['total']:.1f}s")
                if not cfg.is_long and not 45 <= dur <= 60.5:
                    add("video_duration", ERROR, f"video is {dur:.1f}s; Shorts need 45-60s")

    errors = [i for i in issues if i["severity"] == ERROR]
    warns = [i for i in issues if i["severity"] == WARN]
    return {"passed": not errors, "score": max(0, 100 - 25 * len(errors) - 5 * len(warns)),
            "errors": len(errors), "warnings": len(warns), "issues": issues}


def fixable(report, fix):
    return [i for i in report["issues"] if i["fix"] == fix and i["severity"] == ERROR] or \
           [i for i in report["issues"] if i["fix"] == fix]
