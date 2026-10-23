"""Orchestration tests for scheduler_horror.run_horror_cycle with every external service mocked.
Rendering runs real ffmpeg on tiny generated clips (270x480)."""
import json
import os

import pytest

import scheduler_horror as sh
from tests.horror_fakes import (FakeLLM, FakeMeta, FakePexels, SHORT_SENTENCES, fake_synth, make_test_clip)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in list(os.environ):
        if k.startswith("HORROR_"):
            monkeypatch.delenv(k)
    meta, clip = FakeMeta(), make_test_clip(str(tmp_path / "src.mp4"))
    uploads, posted, videos = [], [], []

    def deps(llm=None, pexels=None, synth=fake_synth, can_upload=True, upload=None, fetch=None):
        pexels = pexels or FakePexels(clip)
        return sh.Deps(
            meta_get=meta.get_meta, meta_set=meta.set_meta, llm_invoke=llm or FakeLLM(),
            search=pexels.search, rank=None, download=pexels.download, synth=synth,
            fetch_audio=fetch or (lambda q, d: None),
            upload=upload or (lambda *a: uploads.append(a) or ("vid123", "https://youtu.be/vid123")),
            probe_duration=lambda p: __import__("agents.video_agent", fromlist=["x"]).get_audio_duration(p),
            mark_posted=posted.append, upsert_video=lambda *a: videos.append(a),
            render_kwargs={"width": 270, "height": 480}, can_upload=can_upload)
    return type("Env", (), {"meta": meta, "deps": staticmethod(deps), "uploads": uploads, "posted": posted,
                            "videos": videos, "clip": clip, "pexels": FakePexels})


def _report():
    with open(sh.REPORT_PATH) as f:
        return json.load(f)


def run(env, overrides=None, force=False, **dk):
    return sh.run_horror_cycle({"category": "supernatural", "music": False, "subtitles": False, **(overrides or {})},
                               force=force, deps=env.deps(**dk))


def test_dry_run_renders_valid_short_and_commits_nothing(env):
    llm = FakeLLM()
    res = run(env, {"upload": False}, llm=llm)
    assert res["status"] == "rendered_not_uploaded", res
    assert res["video_path"].endswith(".mp4") and os.path.getsize(res["video_path"]) > 0
    assert res["llm_calls"] == len(llm.calls) == 5            # concept, bible, script, scene plan, metadata
    report = _report()
    assert report["review"]["passed"] and 45 <= report["video"]["probed_duration"] <= 60
    assert report["metadata"]["description"].startswith("Fiction:")
    assert all(a["status"] in ("ok", "reused_same_location") for a in report["assets"])
    # a dry run must not touch caps / clip memory / series / posted history, and clears its run state
    assert not env.uploads and not env.posted
    assert not any(k in env.meta.d for k in (sh.CAP_COUNT_KEY, "horror_used_pexels_ids", "horror_recent"))
    assert not env.meta.d.get("run_state:horror")


def test_full_upload_records_state_and_respects_gates(env):
    res = run(env)
    assert res["status"] == "uploaded" and res["video_url"] == "https://youtu.be/vid123"
    title, desc, hashtags = env.uploads[0][1], env.uploads[0][2], env.uploads[0][3]
    assert "fiction" in desc.lower() and "#Fiction" in hashtags
    assert env.meta.d[sh.CAP_COUNT_KEY] == "1" and json.loads(env.meta.d["horror_used_pexels_ids"])
    assert json.loads(env.meta.d["horror_recent"])["categories"] == ["supernatural"]
    assert env.posted and env.videos[0][0] == "vid123"
    assert not env.meta.d.get("run_state:horror")
    # minimum gap blocks an immediate second upload, --force overrides it
    again = run(env)
    assert again["status"] == "skipped_gate" and "since last upload" in again["reason"]
    assert run(env, force=True)["status"] == "uploaded"


def test_daily_cap_blocks_uploads(env):
    env.meta.d[sh.CAP_DATE_KEY] = sh._now().date().isoformat()
    env.meta.d[sh.CAP_COUNT_KEY] = "2"
    assert run(env)["status"] == "skipped_gate"


def test_no_credentials_means_render_only_not_a_crash(env):
    res = run(env, can_upload=False)
    assert res["status"] == "rendered_not_uploaded" and not env.uploads


def test_missing_footage_stops_before_render_and_flags_scenes(env, tmp_path):
    res = run(env, {"upload": False}, pexels=env.pexels(env.clip, empty=True))
    assert res["status"] == "assets_missing" and "s1" in res["detail"]
    assert not os.path.exists("output/horror_final.mp4")      # nothing rendered, nothing uploaded
    report = _report()
    assert {a["status"] for a in report["assets"]} == {"missing"}
    assert any(i["code"] == "scene_clip_missing" for i in report["review"]["issues"])
    assert env.meta.d.get("run_state:horror")                 # progress is kept so the next run resumes


def test_unrelated_stock_is_never_used_as_filler(env):
    res = run(env, {"upload": False}, pexels=env.pexels(env.clip, unrelated=True))
    assert res["status"] == "assets_missing"


def test_llm_outage_returns_status_and_resumes_without_repeating_work(env):
    llm = FakeLLM()
    state = {"n": 0}

    def flaky(prompt):
        state["n"] += 1
        if "narration script" in prompt and state["n"] < 20 and not state.get("healed"):
            raise RuntimeError("429 quota")
        return llm(prompt)
    res = run(env, {"upload": False}, llm=flaky)
    assert res["status"] == "llm_unavailable"
    state["healed"] = True
    llm2 = FakeLLM()
    res2 = run(env, {"upload": False}, llm=lambda p: llm2(p))
    assert res2["status"] == "rendered_not_uploaded"
    assert not any("horror story editor" in p or "STORY BIBLE" in p for p in llm2.calls)   # concept+bible not redone


def test_llm_budget_cap_is_respected_and_metadata_falls_back(env):
    llm = FakeLLM()
    res = run(env, {"upload": False, "max_llm_calls": 4}, llm=llm)
    assert res["status"] == "rendered_not_uploaded" and len(llm.calls) == 4
    assert _report()["metadata"]["description"].startswith("Fiction:")


def test_resume_after_upload_failure_does_not_regenerate(env):
    boom = {"on": True}

    def upload(*a):
        if boom["on"]:
            raise RuntimeError("youtube 500")
        return "vid9", "https://youtu.be/vid9"
    llm = FakeLLM()
    assert run(env, llm=llm, upload=upload)["status"] == "error"
    first_calls = len(llm.calls)
    boom["on"] = False
    res = run(env, llm=llm, upload=upload)
    assert res["status"] == "uploaded" and len(llm.calls) == first_calls      # zero new LLM calls on resume


def test_episodic_series_carries_bible_and_summary_to_next_episode(env):
    ov = {"mode": "episodic", "series_id": "hostel13"}
    llm1 = FakeLLM()
    assert run(env, ov, llm=llm1)["status"] == "uploaded"
    assert "(Ep 1)" in env.uploads[0][1]
    saved = json.loads(env.meta.d["horror_series:hostel13"])
    assert saved["episode"] == 1 and saved["bible"]["characters"][0]["clothing"] == "black hoodie, blue jeans"
    llm2 = FakeLLM()
    assert run(env, ov, force=True, llm=llm2)["status"] == "uploaded"
    assert "(Ep 2)" in env.uploads[1][1]
    assert any("EPISODE 2" in p and "Mira finds room thirteen" in p for p in llm2.calls)   # previous summary supplied
    assert not any("STORY BIBLE" in p for p in llm2.calls)                                 # bible reused, 1 call saved


def test_over_long_narration_triggers_one_targeted_rewrite_not_a_full_rerun(env):
    slow = lambda text, path, voice, rate, pitch: fake_synth(text + " " + text.split(".")[0] * 0 + " ".join(["pad"] * (len(text.split()) // 2)),
                                                              path, voice, rate, pitch)   # ~1.5x slower speech
    llm = FakeLLM({"Shorten this horror narration": " ".join(SHORT_SENTENCES[:6])})
    res = run(env, {"upload": False, "max_llm_calls": 8}, llm=llm, synth=slow)
    assert res["status"] == "rendered_not_uploaded", res
    assert sum("Shorten this horror narration" in p for p in llm.calls) == 1
    assert 45 <= _report()["video"]["probed_duration"] <= 60


def test_horror_only_writes_horror_keys(env):
    run(env)
    assert all(k.startswith(("horror_", "run_state:horror")) for k in env.meta.d), list(env.meta.d)


def test_sound_design_is_mixed_when_available(env, tmp_path):
    from pydub import AudioSegment
    from agents_bhakti import music_agent
    bed = str(tmp_path / "bed.mp3")
    AudioSegment.silent(duration=3000).export(bed, format="mp3").close()
    rec = {"path": bed, "license": music_agent.CC0, "name": "Fog", "author": "a", "url": "u"}
    res = run(env, {"upload": False, "music": True}, fetch=lambda q, d: rec)
    assert res["status"] == "rendered_not_uploaded"
    report = _report()
    assert not any(i["code"] == "no_ambience" for i in report["review"]["issues"])
