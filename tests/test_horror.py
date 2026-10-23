"""AI CarryON Horror: unit + orchestration tests. Everything external is mocked."""
import json
import os
import subprocess

import pytest

from agents_horror import (clip_agent, reviewer, scene_planner, seo_agent, series, sound_agent, story_agent,
                           video_agent, voice_agent)
from agents_horror.config import HorrorConfig, CATEGORIES, pick_category
from agents_horror.llm import LLMBudget, LLMBudgetExceeded, LLMUnavailable, parse_json
from agents_horror.schemas import StoryBible, Scene, validate_bible, validate_scenes
from tests.horror_fakes import (FakeLLM, FakeMeta, FakePexels, SHORT_SENTENCES, bible_json, fake_synth, make_cfg,
                                make_test_clip, scenes_json, script_json)


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def bible():
    return StoryBible.from_dict(json.loads(bible_json()))


@pytest.fixture
def clip(work):
    return make_test_clip(str(work / "src.mp4"))


# ── config ──────────────────────────────────────────────────────────────────

def test_config_defaults_and_clamping():
    cfg = HorrorConfig()
    assert cfg.validate() == []
    assert (cfg.format, cfg.duration_sec, cfg.language, cfg.channel_name) == ("shorts", 52, "en", "AI CarryON Horror")
    cfg = HorrorConfig(duration_sec=20, intensity=9, language="hi", category="bogus")
    notes = cfg.validate()
    assert cfg.duration_sec == 45 and cfg.intensity == 5 and cfg.language == "en" and cfg.category == ""
    assert any("clamped" in n for n in notes) and any("unknown category" in n for n in notes)
    long_cfg = HorrorConfig(format="long", duration_sec=5000)
    long_cfg.validate()
    assert long_cfg.duration_sec == 1200


def test_config_reads_only_horror_env(monkeypatch):
    monkeypatch.setenv("HORROR_INTENSITY", "5")
    monkeypatch.setenv("HORROR_FORMAT", "long")
    monkeypatch.setenv("HORROR_DURATION", "300")
    monkeypatch.setenv("DURATION", "7")            # a non-Horror variable must be ignored
    monkeypatch.setenv("BHAKTI_MODE", "music_only")
    cfg = HorrorConfig.from_env()
    assert (cfg.intensity, cfg.format, cfg.duration_sec) == (5, "long", 300)
    cfg2 = HorrorConfig.from_env(intensity=2)
    assert cfg2.intensity == 2


def test_category_rotation_prefers_unused():
    assert pick_category(["supernatural", "psychological"]) not in ("supernatural", "psychological")
    full = list(CATEGORIES)
    assert pick_category(full) == full[0]                    # all used -> least recently used
    assert pick_category(full + [full[0]]) == full[1]        # ...and recency is respected


# ── schemas / bible / continuity ────────────────────────────────────────────

def test_bible_validation(bible):
    assert validate_bible(bible) == []
    bible.characters[0].clothing = ""
    assert any("appearance and clothing" in e for e in validate_bible(bible))
    assert any("no locations" in e for e in validate_bible(StoryBible.from_dict({"characters": [{"name": "A"}]})))


def test_scene_validation_catches_bad_refs_order_and_timing(bible):
    ok = Scene("s1", 1, "Hello there.", 0, 3, ["mira"], "hostel_corridor", search_keywords=["dark corridor"], bible_refs=["mira"])
    ok2 = Scene("s2", 2, "More.", 3, 3, ["mira"], "hostel_corridor", search_keywords=["dark corridor"])
    assert validate_scenes([ok, ok2], bible) == []
    bad = Scene("s2", 3, "", 9, 0, ["ghost"], "nowhere", search_keywords=[])
    errs = " | ".join(validate_scenes([ok, bad], bible))
    for needle in ("order 3", "empty narration", "non-positive", "does not follow", "unknown bible ids", "no search_keywords"):
        assert needle in errs


def test_character_continuity_metadata_is_carried_into_scene_refs(bible):
    scenes = scene_planner.plan_scenes(make_cfg(), " ".join(SHORT_SENTENCES), bible,
                                       LLMBudget(8, invoke=FakeLLM()))
    for sc in scenes:
        assert "hostel_corridor" in sc.bible_refs and sc.location == "hostel_corridor"
        assert sc.characters == ["mira"] and "mira" in sc.bible_refs
    assert bible.get("mira").clothing == "black hoodie, blue jeans"
    assert validate_scenes(scenes, bible) == []


# ── LLM budget ──────────────────────────────────────────────────────────────

def test_llm_budget_is_enforced_and_failures_are_typed():
    b = LLMBudget(2, invoke=FakeLLM())
    b.ask("a"); b.ask("b")
    with pytest.raises(LLMBudgetExceeded):
        b.ask("c")
    with pytest.raises(LLMUnavailable):
        LLMBudget(2, invoke=FakeLLM(fail=True)).ask("x")
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1} and parse_json("nothing") is None


# ── script ──────────────────────────────────────────────────────────────────

def test_script_cleaning_and_validation():
    raw = "Hook: The door opened. [thunder] She froze (long pause). **Run.** Now. Then it stopped."
    cleaned = story_agent.clean_script(raw)
    assert "[" not in cleaned and "(" not in cleaned and "Hook:" not in cleaned and "*" not in cleaned
    cfg = make_cfg()
    errs, _ = story_agent.validate_script("It is a true story. " + " ".join(SHORT_SENTENCES), cfg)
    assert any("presents fiction as real" in e for e in errs)
    errs, _ = story_agent.validate_script("Too short. Really. Yes. No.", cfg)
    assert any("too short" in e for e in errs)
    errs, warns = story_agent.validate_script(" ".join(SHORT_SENTENCES), cfg)
    assert errs == []


# ── scene planner ───────────────────────────────────────────────────────────

def test_scene_plan_rebuilds_narration_from_script_and_never_drops_text(bible):
    script = " ".join(SHORT_SENTENCES)
    scenes = scene_planner.plan_scenes(make_cfg(), script, bible, LLMBudget(8, invoke=FakeLLM()))
    assert " ".join(s.narration for s in scenes) == script
    assert [s.order for s in scenes] == list(range(1, len(scenes) + 1))
    assert all(s.search_keywords for s in scenes)


def test_scene_plan_falls_back_when_llm_plan_is_broken(bible):
    script = " ".join(SHORT_SENTENCES)
    llm = FakeLLM({"planning": "not json at all"})
    scenes = scene_planner.plan_scenes(make_cfg(), script, bible, LLMBudget(8, invoke=llm))
    assert " ".join(s.narration for s in scenes) == script
    assert all(s.search_keywords for s in scenes)             # built from the bible location, not a fixed list
    assert all("hostel corridor" in " ".join(s.search_keywords) for s in scenes)
    # budget exhausted -> same deterministic fallback, no exception
    scenes2 = scene_planner.plan_scenes(make_cfg(), script, bible, LLMBudget(0, invoke=FakeLLM()))
    assert len(scenes2) >= 4


def test_retime_replaces_estimates_with_actual_audio_lengths(bible):
    scenes = scene_planner.plan_scenes(make_cfg(), " ".join(SHORT_SENTENCES), bible, LLMBudget(8, invoke=FakeLLM()))
    actual = [7.0] * len(scenes)
    scene_planner.retime_from_actual(scenes, actual)
    assert scenes[0].start == 0 and scenes[1].start == 7.0 and scenes[-1].start == 7.0 * (len(scenes) - 1)


# ── clips / media sourcing ──────────────────────────────────────────────────

def _scenes(bible, n=3):
    return [Scene(f"s{i}", i, f"Line {i}.", (i - 1) * 6, 6, ["mira"], "hostel_corridor",
                  search_keywords=["empty hostel corridor", "dark hallway"], bible_refs=["mira", "hostel_corridor"])
            for i in range(1, n + 1)]


def test_clips_relevant_footage_is_used_and_unique(work, bible, clip):
    fp = FakePexels(clip)
    res = clip_agent.source_clips(_scenes(bible), bible, make_cfg(), lambda k: None,
                                  search=fp.search, rank=None, download=fp.download, folder=str(work / "clips"))
    assert res["missing"] == [] and res["coverage"] == 1.0
    ids = [a["pexels_id"] for a in res["assets"].values()]
    assert len(set(ids)) == 3                                  # no clip repeated inside a video
    assert all(a["relevance"] >= 0.34 and os.path.exists(a["path"]) for a in res["assets"].values())


def test_unrelated_footage_is_rejected_not_inserted(work, bible, clip):
    fp = FakePexels(clip, unrelated=True)
    res = clip_agent.source_clips(_scenes(bible), bible, make_cfg(), lambda k: None,
                                  search=fp.search, rank=None, download=fp.download, folder=str(work / "clips"))
    assert res["missing"] == ["s1", "s2", "s3"] and res["coverage"] == 0.0
    assert all(a["status"] == "missing" and a["path"] == "" for a in res["assets"].values())


def test_empty_search_results_are_flagged_missing(work, bible, clip):
    fp = FakePexels(clip, empty=True)
    res = clip_agent.source_clips(_scenes(bible), bible, make_cfg(), lambda k: None,
                                  search=fp.search, rank=None, download=fp.download, folder=str(work / "clips"))
    assert res["missing"] == ["s1", "s2", "s3"]


def test_same_location_reuse_is_flagged_and_retry_is_targeted(work, bible, clip):
    scenes, cfg = _scenes(bible), make_cfg()

    class OnlyFirst(FakePexels):                      # only the first search succeeds
        def search(self, q, o="portrait"):
            return super().search(q, o) if self.n == 0 else (self.searched.append(q) or [])
    fp = OnlyFirst(clip)
    kw = dict(search=fp.search, rank=None, download=fp.download, folder=str(work / "clips"))
    res = clip_agent.source_clips(scenes, bible, cfg, lambda k: None, **kw)
    assert res["assets"]["s1"]["status"] == "ok"
    assert res["assets"]["s2"]["status"] == "reused_same_location" and "same location" in res["assets"]["s2"]["note"]
    assert res["missing"] == []
    # a later retry only touches scenes that are missing
    fp.searched.clear()
    clip_agent.retry_missing(scenes, bible, cfg, lambda k: None, res, **kw)
    assert fp.searched == []


def test_used_ids_commit_only_when_asked(bible):
    m = FakeMeta()
    assets = {"s1": {"status": "ok", "pexels_id": 5}, "s2": {"status": "missing", "pexels_id": None}}
    assert clip_agent.load_used_ids(m.get_meta) == []
    clip_agent.commit_used_ids(m.get_meta, m.set_meta, assets)
    assert clip_agent.load_used_ids(m.get_meta) == [5]


def test_horror_does_not_touch_other_pipelines_clip_folder():
    assert clip_agent.CLIP_FOLDER == "assets/horror_clips" and clip_agent.USED_IDS_KEY.startswith("horror_")


# ── voice / sound ───────────────────────────────────────────────────────────

def test_voice_per_scene_durations_and_failover(work, bible):
    scenes = _scenes(bible)
    calls = []

    def synth(text, path, voice, rate, pitch):
        calls.append(voice)
        if len(calls) == 1:
            raise RuntimeError("voice 1 down")
        fake_synth(text, path, voice, rate, pitch)
    out = voice_agent.generate_narration(scenes, out_path="output/v.mp3", work_dir="output/vw", synth=synth)
    assert len(out["durations"]) == 3 and abs(out["total"] - sum(out["durations"])) < 0.01
    assert out["speedup"] == 1.0 and os.path.exists(out["path"]) and len(set(calls[:2])) == 2


def test_voice_all_voices_failing_raises(work, bible):
    def boom(*a):
        raise RuntimeError("down")
    with pytest.raises(voice_agent.VoiceError):
        voice_agent.generate_narration(_scenes(bible), out_path="output/v.mp3", work_dir="output/vw", synth=boom)


def test_voice_speeds_up_instead_of_chopping_when_too_long(work, bible):
    scenes = _scenes(bible)
    for sc in scenes:
        sc.narration = "word " * 40     # ~17 s each -> ~52 s raw
    out = voice_agent.generate_narration(scenes, out_path="output/v.mp3", work_dir="output/vw", synth=fake_synth, max_total=45)
    assert 1.0 < out["speedup"] <= 1.15 and out["total"] < 55


def test_sfx_planning_avoids_jump_scare_overload(bible):
    scenes = _scenes(bible, 8)
    for i, sc in enumerate(scenes):
        sc.sfx, sc.start = ["door creak"], i * 3.0          # a cue in EVERY scene, only 3 s apart
    cues = sound_agent.plan_sfx(scenes, max_sfx=4, intensity=3)
    assert len(cues) <= 4 and all(c["at"] >= 2.0 for c in cues)
    assert all(b["at"] - a["at"] >= 6.0 for a, b in zip(cues, cues[1:]))


def test_sound_design_without_key_degrades_gracefully(bible):
    cfg = make_cfg(music=True)
    out = sound_agent.build_sound_design(cfg, _scenes(bible), bible, 40, fetch=lambda q, d: None)
    assert out["bed"] is None and out["sfx"] == [] and out["notes"]
    off = sound_agent.build_sound_design(make_cfg(music=False), _scenes(bible), bible, 40)
    assert off["bed"] is None and "disabled" in off["notes"][0]


def test_cc_by_sound_requires_attribution(work, bible):
    from agents_bhakti import music_agent
    p = str(work / "bed.mp3")
    with open(p, "wb") as f:
        f.write(b"x")
    rec = {"path": p, "license": music_agent.CC_BY, "name": "Rain", "author": "someone", "url": "https://freesound.org/s/1/"}
    out = sound_agent.build_sound_design(make_cfg(music=True), _scenes(bible), bible, 40, fetch=lambda q, d: rec)
    assert out["attributions"] and "CC BY" in sound_agent.attribution_lines(out["attributions"])[0]


# ── render (real ffmpeg, tiny clips) ────────────────────────────────────────

def test_video_assembly_with_local_assets(work, bible, clip):
    scenes = _scenes(bible)
    for sc in scenes:
        sc.duration, sc.pause_after = 2.0, 0.3
    from pydub import AudioSegment
    AudioSegment.silent(duration=6000).export("output_voice.mp3", format="mp3").close()
    assets = {sc.scene_id: {"status": "ok", "path": clip} for sc in scenes}
    scene_planner.retime_from_actual(scenes, [2.0, 2.0, 2.0])
    out = video_agent.render_video(scenes, assets, "output_voice.mp3", {"bed": None, "sfx": []}, subtitles=True,
                                   out_path="output/final.mp4", width=270, height=480)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
                            "-of", "csv=p=0", out["path"]], capture_output=True, text=True).stdout
    assert "h264,270,480" in probe and "aac" in probe
    assert abs(out["duration"] - 6.0) < 0.01 and os.path.getsize(out["path"]) > 0


def test_render_refuses_scene_without_clip(work, bible, clip):
    scenes = _scenes(bible, 2)
    assets = {"s1": {"status": "ok", "path": clip}, "s2": {"status": "missing", "path": ""}}
    with pytest.raises(video_agent.RenderError, match="no usable clip"):
        video_agent.render_video(scenes, assets, "x.mp3", {}, out_path="output/f.mp4", width=270, height=480)


def test_srt_uses_real_scene_timings(work, bible):
    scenes = _scenes(bible, 2)
    scene_planner.retime_from_actual(scenes, [4.0, 5.0])
    with open(video_agent.build_srt(scenes, "output/c.srt")) as f:
        srt = f.read()
    assert "00:00:00,000 -->" in srt and "00:00:04,000 -->" in srt


# ── reviewer ────────────────────────────────────────────────────────────────

def _review(bible, cfg=None, **kw):
    cfg = cfg or make_cfg()
    scenes = kw.pop("scenes", None) or scene_planner.plan_scenes(cfg, " ".join(SHORT_SENTENCES), bible,
                                                                  LLMBudget(8, invoke=FakeLLM()))
    return reviewer.review(cfg, kw.pop("script", " ".join(SHORT_SENTENCES)), bible, scenes, **kw), scenes


def test_reviewer_flags_missing_assets_with_targeted_fix(bible):
    report, scenes = _review(bible, assets={})
    codes = {i["code"]: i for i in report["issues"]}
    assert not report["passed"] and codes["scene_clip_missing"]["fix"] == "resource_scene"


def test_reviewer_flags_bad_hook_duration_sfx_and_fiction_claims(bible, work):
    long_hook = "This is a story that has an extremely long opening sentence which goes on and on and never ends. " + " ".join(SHORT_SENTENCES)
    voice = {"total": 38.0, "speedup": 1.0}
    sound = {"bed": None, "sfx": [{"at": 1.0}, {"at": 3.0}]}
    meta = {"title": "A true story", "description": "Scary."}
    report, _ = _review(bible, script=long_hook, voice=voice, sound=sound, metadata=meta)
    codes = {i["code"] for i in report["issues"]}
    assert {"hook_long", "narration_duration", "sfx_early", "sfx_crowded", "no_fiction_label", "claims_real"} <= codes
    assert report["score"] < 50 and not report["passed"]


def test_reviewer_passes_clean_package(bible, work, clip):
    cfg = make_cfg()
    scenes = scene_planner.plan_scenes(cfg, " ".join(SHORT_SENTENCES), bible, LLMBudget(8, invoke=FakeLLM()))
    assets = {s.scene_id: {"status": "ok", "path": clip, "relevance": 1.0} for s in scenes}
    with open("v.mp4", "wb") as f:
        f.write(b"x")
    report = reviewer.review(cfg, " ".join(SHORT_SENTENCES), bible, scenes, assets=assets,
                             voice={"total": 50.0}, sound={"bed": {"path": "x"}, "sfx": [{"at": 10.0}, {"at": 30.0}]},
                             video={"path": "v.mp4", "probed_duration": 50.2},
                             metadata={"title": "Room Thirteen", "description": "Fiction: A work of fiction."})
    assert report["passed"], report["issues"]


def test_fiction_label_itself_is_not_flagged_as_a_real_claim(bible, work, clip):
    from agents_horror.config import FICTION_LABEL
    report, _ = _review(bible, metadata={"title": "Room Thirteen", "description": f"Fiction: {FICTION_LABEL}\n\nA door."})
    assert not any(i["code"] == "claims_real" for i in report["issues"])


# ── metadata ────────────────────────────────────────────────────────────────

def test_metadata_always_labels_fiction_and_strips_real_claims(bible):
    cfg = make_cfg()
    concept = {"premise": "A student finds a locked room."}
    llm = FakeLLM({"YouTube metadata": json.dumps({"title": "TRUE STORY: the hostel room", "description": "This really happened!",
                                                   "tags": ["x"], "thumbnail_text": "ROOM 13", "thumbnail_concept": "door"})})
    m = seo_agent.generate_metadata(cfg, concept, bible, "script", LLMBudget(5, invoke=llm))
    assert "fiction" in m["description"].lower() and "true story" not in m["title"].lower()
    assert "#Fiction" in m["hashtags"] and len(m["title"]) <= 60


def test_metadata_fallback_when_llm_unavailable_and_episode_numbering(bible):
    cfg = make_cfg(mode="episodic")
    m = seo_agent.generate_metadata(cfg, {"premise": "A student finds a locked room."}, bible, "s",
                                    LLMBudget(5, invoke=FakeLLM(fail=True)), episode=3, series_title="Hostel 13")
    assert m["title"].endswith("(Ep 3)") and m["episode"] == 3 and "Episode 3" in m["description"]


def test_research_is_optional_and_marks_fictional_additions():
    from agents_horror.research_agent import research_notes
    concept = {"research_topic": "Bhangarh Fort"}
    assert research_notes(make_cfg(research=False), concept)["used"] is False

    class R:
        status_code = 200
        def json(self):
            return {"title": "Bhangarh Fort", "extract": "A fort in Rajasthan. It dates to the 17th century. It is a ruin.",
                    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Bhangarh_Fort"}}}
    notes = research_notes(make_cfg(research=True), concept, http_get=lambda url: R())
    assert notes["used"] and notes["sources"][0]["url"].startswith("https://en.wikipedia.org") and notes["fictional_additions"]
    assert research_notes(make_cfg(research=True), concept, http_get=lambda url: (_ for _ in ()).throw(OSError("down")))["used"] is False


def test_series_memory_and_similarity():
    m = FakeMeta()
    series.save_episode(m.get_meta, m.set_meta, "h13", "Room Thirteen", 1, "Mira finds the room.", {"title": "x"})
    s = series.load_series(m.get_meta, "h13")
    assert s["episode"] == 1 and s["summaries"][0]["summary"] == "Mira finds the room." and s["bible"] == {"title": "x"}
    assert series.similarity("a locked hostel room waiting", "a locked hostel room waiting") == 1.0
    assert series.similarity("haunted lighthouse keeper", "locked hostel room student") < 0.2
