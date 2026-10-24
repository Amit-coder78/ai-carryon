# scheduler_horror.py — single-run entrypoint for AI CarryON Horror (6th pipeline)
"""
One full generate (+ optional upload) attempt per invocation, like scheduler_cricket.py /
scheduler_bhakti.py. Triggered by .github/workflows/horror-scheduler.yml or run locally:

    python scheduler_horror.py                         # rotate category, 52 s Short, upload if token exists
    python scheduler_horror.py --no-upload --category creature --intensity 4
    python scheduler_horror.py --format long --duration 300 --mode episodic --series hostel13

Stages: concept -> research (optional) -> story bible -> script -> scene plan -> narration (per scene)
-> Pexels clips (same integration as Cricket) -> sound design -> render -> review -> metadata -> upload.
Every step is saved with agents.run_state.RunState, so a stopped run resumes where it stopped.
"""
import os
import sys
import json
import traceback
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Callable, Optional

try:  # same per-channel key -> shared key fallback every other scheduler installs
    import key_fallback
    key_fallback.install()
except Exception as _e:  # pragma: no cover
    print(f"key_fallback unavailable: {_e}")
from dotenv import load_dotenv
load_dotenv()

from agents_horror.config import HorrorConfig, CATEGORIES, CHANNEL, pick_category
from agents_horror.llm import LLMBudget, LLMBudgetExceeded, LLMUnavailable
from agents_horror import series as series_mod
from agents_horror.schemas import StoryBible, Scene, validate_bible, validate_scenes

REPORT_PATH = "output/horror_report.json"
LAST_UPLOAD_KEY = "horror_last_upload_at"
CAP_DATE_KEY, CAP_COUNT_KEY = "horror_upload_date", "horror_upload_count"


class PipelineStop(Exception):
    """A deliberate, reportable stop (not a crash). `status` becomes the run result."""
    def __init__(self, status, detail=""):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


@dataclass
class Deps:
    """Everything external, injectable so tests can run with mocks (no API calls, no charges)."""
    meta_get: Optional[Callable] = None
    meta_set: Optional[Callable] = None
    llm_invoke: Optional[Callable] = None
    search: Optional[Callable] = None
    rank: Optional[Callable] = None
    download: Optional[Callable] = None
    synth: Optional[Callable] = None
    fetch_audio: Optional[Callable] = None
    upload: Optional[Callable] = None
    probe_duration: Optional[Callable] = None
    mark_posted: Optional[Callable] = None
    upsert_video: Optional[Callable] = None
    veo_generate: Optional[Callable] = None
    render_kwargs: dict = field(default_factory=dict)
    can_upload: Optional[bool] = None

    def resolve(self):
        if self.meta_get is None or self.meta_set is None:
            from agents.database import db
            self.meta_get, self.meta_set = db.get_meta, db.set_meta
            self.mark_posted = self.mark_posted or (lambda topic: db.mark_posted(topic, channel=CHANNEL))
            self.upsert_video = self.upsert_video or (
                lambda vid, title, pub: db.upsert_video(vid, title, pub, channel=CHANNEL))
        self.mark_posted = self.mark_posted or (lambda topic: None)
        self.upsert_video = self.upsert_video or (lambda vid, title, pub: None)
        if self.probe_duration is None:
            from agents.video_agent import get_audio_duration
            self.probe_duration = get_audio_duration
        if self.upload is None:
            from agents_horror import upload_agent
            self.upload = upload_agent.upload_video
            if self.can_upload is None:
                self.can_upload = upload_agent.has_credentials()
        if self.can_upload is None:
            self.can_upload = True
        return self


# ── gates (daily cap + minimum gap; only for real uploads) ───────────────────

def _now():
    return datetime.now(timezone.utc)


def check_gates(cfg, meta_get, now=None):
    """Returns (ok, reason)."""
    now = now or _now()
    try:
        today = now.date().isoformat()
        count = int(meta_get(CAP_COUNT_KEY) or 0) if meta_get(CAP_DATE_KEY) == today else 0
        if count >= cfg.daily_cap:
            return False, f"daily cap reached ({count}/{cfg.daily_cap})"
        last = meta_get(LAST_UPLOAD_KEY)
        if last:
            gap_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
            if gap_h < cfg.min_gap_hours:
                return False, f"only {gap_h:.1f}h since last upload (min {cfg.min_gap_hours}h)"
    except Exception as e:
        print(f"[horror] gate check failed, allowing run: {e}")
    return True, ""


def _bump_cap(meta_get, meta_set, now=None):
    now = now or _now()
    today = now.date().isoformat()
    count = int(meta_get(CAP_COUNT_KEY) or 0) if meta_get(CAP_DATE_KEY) == today else 0
    meta_set(CAP_DATE_KEY, today)
    meta_set(CAP_COUNT_KEY, str(count + 1))
    meta_set(LAST_UPLOAD_KEY, now.isoformat())


# ── the pipeline ─────────────────────────────────────────────────────────────

def run_horror_cycle(overrides=None, force=False, deps=None):
    try:
        return _run_inner(overrides or {}, force, (deps or Deps()).resolve())
    except PipelineStop as stop:
        print(f"HORROR PIPELINE STOPPED: {stop}")
        return {"status": stop.status, "detail": stop.detail}
    except (LLMUnavailable, LLMBudgetExceeded) as e:
        print(f"HORROR PIPELINE: LLM problem: {e} (progress is saved; the next run resumes)")
        return {"status": "llm_unavailable", "error": str(e)}
    except Exception as e:
        print(f"HORROR PIPELINE CRASHED: {e}")
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def _run_inner(overrides, force, d):
    from agents.run_state import RunState
    from agents_horror import (story_agent, research_agent, scene_planner, clip_agent, voice_agent,
                               sound_agent, video_agent, reviewer, seo_agent)

    state = RunState.open(CHANNEL, d.meta_get, d.meta_set)
    if state.resumed:
        cfg = HorrorConfig(**{**state.ctx["cfg"], "extras": state.ctx["cfg"].get("extras") or {}})
        print("Resuming a Horror run that stopped earlier")
    else:
        cfg = HorrorConfig.from_env(**overrides)
        if cfg.upload and not force:
            ok, reason = check_gates(cfg, d.meta_get)
            if not ok:
                return {"status": "skipped_gate", "reason": reason}
        recent = series_mod.load_recent(d.meta_get)
        category = cfg.category or pick_category(recent["categories"])
        ser = series_mod.load_series(d.meta_get, cfg.series_id) if cfg.mode == "episodic" else None
        episode = (ser["episode"] + 1) if ser else None
        state.begin({"cfg": cfg.to_dict(), "category": category, "episode": episode})
    category, episode = state.ctx["category"], state.ctx.get("episode")
    ser = series_mod.load_series(d.meta_get, cfg.series_id) if cfg.mode == "episodic" else None
    recent = series_mod.load_recent(d.meta_get)
    budget = LLMBudget(cfg.max_llm_calls, invoke=d.llm_invoke)
    print(f"AI CarryON Horror | category={category} format={cfg.format} {cfg.duration_sec}s "
          f"intensity={cfg.intensity} mode={cfg.mode}" + (f" episode={episode}" if episode else ""))

    # Stage 1 — concept (re-asked once if it repeats a recent premise)
    def _concept():
        c = story_agent.generate_concept(cfg, category, budget, recent["premises"], ser)
        if any(series_mod.similarity(c["premise"], p) >= 0.6 for p in recent["premises"]) and budget.remaining > 4:
            print("[horror] premise too close to a recent video — asking once more")
            c = story_agent.generate_concept(cfg, category, budget, recent["premises"] + [c["premise"]], ser)
        return c
    concept = state.step("concept", _concept)

    # Stage 2 — research (optional, no LLM call)
    research = state.step("research", lambda: research_agent.research_notes(cfg, concept))

    # Stage 4 — story bible (before any scene planning)
    def _bible():
        return story_agent.build_bible(cfg, concept, budget, ser).to_dict()
    bible = StoryBible.from_dict(state.step("bible", _bible))
    if validate_bible(bible):
        state.forget("bible")
        raise PipelineStop("bible_invalid", "; ".join(validate_bible(bible)))

    # Stage 3 — script (one length-fix rewrite allowed)
    def _script():
        script, summary = story_agent.generate_script(cfg, concept, bible, budget, episode, research)
        errs, _ = story_agent.validate_script(script, cfg)
        if errs and any("too short" in e or "too long" in e for e in errs) and budget.remaining > 2:
            script = story_agent.rewrite_to_length(cfg, script, budget)
            errs, _ = story_agent.validate_script(script, cfg)
        if errs:
            raise PipelineStop("script_rejected", "; ".join(errs))
        return {"script": script, "summary": summary}
    script_d = state.step("script", _script)

    # Stage 5 — scene plan
    def _scenes():
        scenes = scene_planner.plan_scenes(cfg, script_d["script"], bible, budget)
        return [s.to_dict() for s in scenes]
    scenes_l = state.step("scenes", _scenes, after=["script"])

    # Stage 8 — narration, one clip per scene (real durations drive the whole edit)
    for attempt in (0, 1):
        scenes = [Scene.from_dict(x) for x in scenes_l]
        voice = state.step(
            "voice",
            lambda: voice_agent.generate_narration(scenes, synth=d.synth,
                                                   max_total=None if cfg.is_long else voice_agent.MAX_SHORTS_SEC),
            validate=lambda v: os.path.exists(v["path"]), after=["scenes"])
        raw = voice["total"] * voice.get("speedup", 1.0)
        if cfg.is_long or 45 <= voice["total"] <= 60 or attempt == 1 or state.ctx.get("narration_rewritten"):
            break
        state.update_ctx(narration_rewritten=True)  # at most ONE rewrite per run, even across resumes
        # Targeted fix: rewrite the script to the right length and re-time. No clips/video exist yet.
        new_words = int(len(script_d["script"].split()) * 52.5 / max(raw, 1.0))
        print(f"[horror] narration is {voice['total']:.1f}s — rewriting script to ~{new_words} words")
        rewritten = story_agent.rewrite_to_length(cfg, script_d["script"], budget, target_words=new_words)
        script_d = state.set("script", {**script_d, "script": rewritten})
        scenes_l = state.set("scenes", [s.to_dict() for s in scene_planner.plan_scenes(cfg, rewritten, bible, budget)])
        state.forget("voice", "timed")

    def _timed():
        timed = scene_planner.retime_from_actual(scenes, voice["durations"])
        return [s.to_dict() for s in timed]
    timed_l = state.step("timed", _timed, after=["voice", "scenes"])
    scenes = [Scene.from_dict(x) for x in timed_l]
    plan_errors = validate_scenes(scenes, bible)
    if plan_errors:
        raise PipelineStop("scene_plan_invalid", "; ".join(plan_errors[:5]))

    # Stage 6/7 — visuals (Pexels, same integration as Cricket) with targeted retries
    def _clips():
        kw = dict(search=d.search, rank=d.rank, download=d.download)
        res = clip_agent.source_clips(scenes, bible, cfg, d.meta_get, **kw)
        res = clip_agent.retry_missing(scenes, bible, cfg, d.meta_get, res, **kw)
        return clip_agent.veo_fill(scenes, bible, cfg, res, generate=d.veo_generate)
    clips = state.step(
        "clips", _clips, after=["timed"],
        validate=lambda r: all(os.path.exists(a["path"]) for a in r["assets"].values() if a.get("path")))
    assets = clips["assets"]

    pre = reviewer.review(cfg, script_d["script"], bible, scenes, assets=assets, voice=voice,
                          premise=concept["premise"], recent_premises=recent["premises"])
    if clips["missing"]:
        _write_report(cfg, concept, bible, scenes, assets, voice, None, pre, None, None, budget)
        raise PipelineStop("assets_missing", f"no relevant footage for scenes {clips['missing']} "
                                             f"(flagged in {REPORT_PATH}; nothing rendered or uploaded)")
    blocking = [i for i in pre["issues"] if i["severity"] == "error"]
    if blocking:
        _write_report(cfg, concept, bible, scenes, assets, voice, None, pre, None, None, budget)
        raise PipelineStop("needs_review", "; ".join(i["message"] for i in blocking[:3]))

    # Stage 8 — sound design (Freesound CC0/CC-BY, optional)
    sound = state.step("sound", lambda: sound_agent.build_sound_design(cfg, scenes, bible, voice["total"], fetch=d.fetch_audio),
                       validate=lambda s: all(os.path.exists(x["path"]) for x in ([s["bed"]] if s.get("bed") else []) + s["sfx"]),
                       after=["timed"])

    # Stage 9 — render
    def _render():
        out = video_agent.render_video(scenes, assets, voice["path"], sound, subtitles=cfg.subtitles, **d.render_kwargs)
        out["probed_duration"] = d.probe_duration(out["path"])
        return out
    video = state.step("video", _render, validate=lambda v: os.path.exists(v["path"]),
                       after=["timed", "clips", "sound"])

    # Section 6 — metadata
    meta = state.step("metadata", lambda: seo_agent.generate_metadata(
        cfg, concept, bible, script_d["script"], budget, episode=episode, series_title=bible.title,
        research=research, attributions=sound["attributions"]), after=["script", "sound"])

    # Stage 10 — review of the finished video (report only; no full re-render for minor issues)
    final = reviewer.review(cfg, script_d["script"], bible, scenes, assets=assets, voice=voice, sound=sound,
                            video=video, metadata=meta, premise=concept["premise"], recent_premises=recent["premises"])
    _write_report(cfg, concept, bible, scenes, assets, voice, sound, final, meta, video, budget)
    print(f"Review: score={final['score']} errors={final['errors']} warnings={final['warnings']}")
    if not final["passed"]:
        raise PipelineStop("needs_review", "; ".join(i["message"] for i in final["issues"] if i["severity"] == "error"))

    result = {"status": "rendered_not_uploaded", "title": meta["title"], "video_path": video["path"],
              "report": REPORT_PATH, "category": category, "llm_calls": budget.calls}
    if not (cfg.upload and d.can_upload):
        why = "dry run (--no-upload / HORROR_UPLOAD=0)" if not cfg.upload else "no YOUTUBE_HORROR_TOKEN_B64"
        print(f"Not uploading: {why}")
        state.clear()  # a dry run never counts toward series/caps/clip-memory
        return result

    video_id, video_url = state.step("upload", lambda: list(d.upload(
        video["path"], meta["title"], meta["description"], meta["hashtags"], meta["tags"])))
    print(f"Uploaded: {video_url}")

    # Only after a successful upload: remember clips, premise, episode, cap
    clip_agent.commit_used_ids(d.meta_get, d.meta_set, assets)
    series_mod.record_recent(d.meta_set, d.meta_get, category, concept["premise"], concept.get("setting", ""))
    if cfg.mode == "episodic" and episode:
        series_mod.save_episode(d.meta_get, d.meta_set, cfg.series_id, bible.title, episode,
                                script_d["summary"], bible.to_dict())
    d.mark_posted(concept["premise"][:200])
    d.upsert_video(video_id, meta["title"], _now().isoformat())
    _bump_cap(d.meta_get, d.meta_set)
    state.clear()
    return {**result, "status": "uploaded", "video_url": video_url}


def _write_report(cfg, concept, bible, scenes, assets, voice, sound, review, meta, video, budget):
    """Human-readable run report: what was used, what is missing, what the reviewer thinks."""
    try:
        os.makedirs("output", exist_ok=True)
        report = {
            "generated_at": _now().isoformat(), "config": cfg.to_dict(), "concept": concept,
            "bible": bible.to_dict(), "scenes": [s.to_dict() for s in scenes],
            "assets": [{k: a.get(k) for k in ("scene_id", "status", "source", "pexels_id", "query", "relevance", "note")}
                       for a in (assets or {}).values()],
            "narration": {k: voice[k] for k in ("total", "voice", "speedup")} if voice else None,
            "sound_notes": (sound or {}).get("notes", []), "review": review, "metadata": meta,
            "video": {k: video[k] for k in ("path", "duration", "probed_duration") if k in (video or {})} if video else None,
            "llm_calls": budget.calls,
        }
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Could not write report: {e}")


def get_posted_count_today():
    """Used by the dashboard tab."""
    from agents.database import db
    return len(db.get_recent_posted(hours=24, channel=CHANNEL))


def _parse_args(argv):
    import argparse
    p = argparse.ArgumentParser(description="AI CarryON Horror — generate (and upload) one horror video")
    p.add_argument("--category", choices=sorted(CATEGORIES))
    p.add_argument("--format", choices=["shorts", "long"])
    p.add_argument("--duration", type=int)
    p.add_argument("--intensity", type=int)
    p.add_argument("--setting")
    p.add_argument("--mode", choices=["standalone", "episodic"])
    p.add_argument("--series")
    p.add_argument("--idea")
    p.add_argument("--no-upload", action="store_true", help="render only (dry run)")
    p.add_argument("--no-music", action="store_true")
    p.add_argument("--no-subtitles", action="store_true")
    p.add_argument("--research", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore the daily cap / minimum gap")
    return p.parse_args(argv)


def _overrides_from_args(a):
    o = {"category": a.category, "format": a.format, "duration_sec": a.duration, "intensity": a.intensity,
         "setting": a.setting, "mode": a.mode, "series_id": a.series, "idea": a.idea}
    if a.no_upload:
        o["upload"] = False
    if a.no_music:
        o["music"] = False
    if a.no_subtitles:
        o["subtitles"] = False
    if a.research:
        o["research"] = True
    return {k: v for k, v in o.items() if v is not None}


def _comment_agent_job():
    """Comment agent (drafts for owner review; gated by COMMENT_AGENT_* settings). Never raises."""
    try:
        from comment_engine.service import run_scheduled
        run_scheduled("horror")
    except Exception as e:
        print(f"Comment agent job error: {e}")


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    print(run_horror_cycle(_overrides_from_args(args), force=args.force))
    _comment_agent_job()
