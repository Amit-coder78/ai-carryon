"""Gaming V2 Sprint 1 tests — all LLM/vision/Twitch calls are mocked."""
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agents.model_invoke_agent_english as llm
from agents_gaming import clip_scorer, commentary_agent, hook_agent, moment_analyzer
from agents_gaming import script_agent, script_quality_agent, v2_utils

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _clip(cid, views, age_h, title="a clip", game="g1", dur=25, kind="top_game", who="x"):
    return {"id": cid, "view_count": views, "duration": dur, "title": title, "game_id": game,
            "broadcaster_name": who, "_topic_type": kind,
            "created_at": (NOW - timedelta(hours=age_h)).isoformat().replace("+00:00", "Z")}


def _fake_llm(monkeypatch, replies):
    """replies: list of strings (or callables(prompt)->str), consumed in order; last repeats."""
    calls = []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=r(prompt) if callable(r) else r)

    monkeypatch.setattr(llm, "safe_invoke", fake)
    monkeypatch.setattr(script_agent, "safe_invoke", fake)   # legacy script_agent binds the name at import
    monkeypatch.setattr(llm, "_provider_dead", lambda name: False)
    monkeypatch.setattr(llm, "_try_gemini", lambda prompt: fake(prompt))
    monkeypatch.setattr(llm, "_get_gemini", lambda *a, **k: SimpleNamespace(invoke=lambda prompt: fake(prompt)))  # pinned judge
    return calls


MOMENT = {"game": "Apex Legends", "moment_type": "CLUTCH",
          "what_happened": "Streamer survives a 3v1 with 1 HP", "setup": "pushed by 3 players",
          "payoff": "wins the fight", "reaction": "screams", "key_timestamp": 12.0,
          "intensity": 9, "funny_score": 2, "clutch_score": 9}
SUMMARY = "Clip title: 1 HP clutch\nStreamer: x\nGame: Apex Legends"


# ── v2_utils ────────────────────────────────────────────────────────────────

def test_extract_json_handles_fences_and_chatter():
    assert v2_utils.extract_json('sure!\n```json\n{"a": 1, "b": {"c": 2}}\n```') == {"a": 1, "b": {"c": 2}}
    assert v2_utils.extract_json("no json here") is None


def test_ungrounded_numbers():
    assert v2_utils.ungrounded_numbers("he got 5 kills with 1 HP", "1 HP clutch") == ["5"]
    assert v2_utils.ungrounded_numbers("no digits at all", "x") == []


def test_repeated_ai_phrases_rules():
    assert v2_utils.repeated_ai_phrases("Watch what happens next", []) == []          # one use, first time: ok
    assert v2_utils.repeated_ai_phrases("Watch what happens next", ["watch what happens"]) == ["watch what happens"]
    two = v2_utils.repeated_ai_phrases("You won't believe it. Absolutely incredible.", [])
    assert set(two) == {"you won't believe", "absolutely incredible"}                  # two in one script: regen


# ── clip_scorer ─────────────────────────────────────────────────────────────

def test_weights_sum_to_one():
    assert sum(clip_scorer.WEIGHTS.values()) == pytest.approx(1.0)


def test_velocity_beats_raw_views():
    old_big = _clip("old", 50000, 20)     # 2.5k views/h
    new_fast = _clip("new", 20000, 1)     # 20k views/h
    filler = [_clip(f"f{i}", 100 * i, 10) for i in range(1, 6)]
    ranked = clip_scorer.rank_clips_v2([old_big, new_fast] + filler, now=NOW)
    assert ranked[0]["id"] == "new"


def test_posted_and_rejected_excluded():
    clips = [_clip("a", 1000, 2), _clip("b", 900, 2), _clip("c", 800, 2)]
    ranked = clip_scorer.rank_clips_v2(clips, already_posted_ids={"a"}, rejected_ids={"b"}, now=NOW)
    assert [c["id"] for c in ranked] == ["c"]
    assert clip_scorer.rank_clips_v2([], now=NOW) == []


def test_analysed_moment_can_lift_a_lower_clip():
    a = _clip("a", 1000, 3, title="boring")
    b = _clip("b", 900, 3, title="boring")          # adjacent to `a` in a realistic pool
    filler = [_clip(f"f{i}", 100 * i, 3, title="boring") for i in range(1, 9)]
    pool = [a, b] + filler
    base = clip_scorer.rank_clips_v2(pool, now=NOW)
    assert base[0]["id"] == "a"
    lifted = clip_scorer.rank_clips_v2(pool, moments={"b": {"intensity": 10, "funny_score": 8, "clutch_score": 10}}, now=NOW)
    assert lifted[0]["id"] == "b"
    assert lifted[0]["_score_parts"]["moment_analysed"] is True


def test_heuristic_moment_quality_capped_below_ten():
    assert clip_scorer.heuristic_moment_quality({"title": "INSANE CLUTCH 1v5 ACE NO WAY!!"}) <= 7.0
    assert clip_scorer.heuristic_moment_quality({"title": "playing games"}) < 5


def test_followed_streamer_relevance():
    a = _clip("a", 1000, 3, kind="streamer")
    b = _clip("b", 1000, 3, kind="top_game")
    ranked = clip_scorer.rank_clips_v2([a, b], now=NOW)
    assert ranked[0]["_score_parts"]["streamer_relevance"] == 10.0
    assert ranked[1]["_score_parts"]["streamer_relevance"] == 4.0


def test_duration_fit_penalises_long_clips():
    assert clip_scorer._duration_fit(20) == 10.0
    assert clip_scorer._duration_fit(60) < clip_scorer._duration_fit(40) < 10.0


# ── moment_analyzer ─────────────────────────────────────────────────────────

def test_normalize_moment_validates_and_clamps():
    raw = {"game": "Apex", "moment_type": "clutch", "what_happened": "wins 1v3", "key_timestamp": 999,
           "intensity": 14, "funny_score": -3, "clutch_score": "8"}
    m = moment_analyzer.normalize_moment(raw, {"duration": 20})
    assert m["moment_type"] == "CLUTCH" and m["intensity"] == 10 and m["funny_score"] == 0
    assert m["clutch_score"] == 8 and m["key_timestamp"] == 20.0
    assert moment_analyzer.normalize_moment({"moment_type": "NOPE", "what_happened": "x"}, {}) is None
    assert moment_analyzer.normalize_moment({"moment_type": "FAIL", "what_happened": ""}, {}) is None
    assert moment_analyzer.normalize_moment("junk", {}) is None


def test_heuristic_moment_shape():
    m = moment_analyzer.heuristic_moment({"title": "Streamer RAGES at lag", "duration": 20})
    assert m["moment_type"] == "RAGE" and m["intensity"] == 5
    assert moment_analyzer.normalize_moment(m, {"duration": 20}) is not None


def test_analyze_falls_through_vision_text_heuristic(monkeypatch, tmp_path):
    clip = {"title": "oops he whiffed", "duration": 20}
    vid = tmp_path / "c.mp4"
    vid.write_bytes(b"x")
    monkeypatch.setattr(moment_analyzer, "probe_duration", lambda p: 20.0)
    monkeypatch.setattr(moment_analyzer, "extract_frames", lambda p: [(5.0, str(vid))])

    good = json.dumps({"game": "CS", "moment_type": "FAIL", "what_happened": "misses an easy shot", "intensity": 6})
    monkeypatch.setattr(moment_analyzer, "_vision_call", lambda f, c: SimpleNamespace(content=good))
    m = moment_analyzer.analyze_moment(clip, str(vid))
    assert m["source"] == "vision" and m["moment_type"] == "FAIL"

    def boom(f, c):
        raise RuntimeError("vision down")
    monkeypatch.setattr(moment_analyzer, "_vision_call", boom)
    monkeypatch.setattr(moment_analyzer, "_text_call", lambda c: SimpleNamespace(content=good))
    assert moment_analyzer.analyze_moment(clip, str(vid))["source"] == "text"

    monkeypatch.setattr(moment_analyzer, "_text_call", lambda c: (_ for _ in ()).throw(RuntimeError("llm down")))
    m = moment_analyzer.analyze_moment(clip, str(vid))
    assert m["source"] == "heuristic" and m["moment_type"] == "FAIL"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_extract_frames_from_real_video(tmp_path):
    vid = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=15",
                    "-pix_fmt", "yuv420p", str(vid)], capture_output=True, check=True)
    assert moment_analyzer.probe_duration(str(vid)) == pytest.approx(6.0, abs=0.2)
    frames = moment_analyzer.extract_frames(str(vid), n=4, out_dir=str(tmp_path))
    assert len(frames) == 4
    assert all(os.path.getsize(p) > 0 for _, p in frames)
    assert [t for t, _ in frames] == sorted(t for t, _ in frames)


# ── hook_agent ──────────────────────────────────────────────────────────────

def test_hook_picks_best_valid_and_filters_bad(monkeypatch):
    hooks = {"hooks": [
        {"type": "CURIOSITY", "text": "Watch the health bar.", "score": 7},
        {"type": "SHOCK", "text": "This guy survived with 1 HP.", "score": 9},
        {"type": "STORY", "text": "He killed 7 players alone here.", "score": 10},     # invented number
        {"type": "REACTION", "text": "You won't believe this clutch.", "score": 10},    # AI cliche
        {"type": "CHALLENGE", "text": "Hey guys welcome back to the channel today.", "score": 10},  # greeting
    ]}
    _fake_llm(monkeypatch, [json.dumps(hooks)])
    best = hook_agent.generate_hooks(MOMENT, SUMMARY)
    assert best["type"] == "SHOCK" and best["score"] == 9
    assert [c["type"] for c in best["candidates"]] == ["SHOCK", "CURIOSITY"]


def test_hook_returns_none_on_llm_failure_or_garbage(monkeypatch):
    _fake_llm(monkeypatch, [RuntimeError("down")])
    assert hook_agent.generate_hooks(MOMENT, SUMMARY) is None
    _fake_llm(monkeypatch, ["not json"])
    assert hook_agent.generate_hooks(MOMENT, SUMMARY) is None


def test_hook_nudges_away_from_last_type(monkeypatch):
    hooks = {"hooks": [{"type": "SHOCK", "text": "He survived with 1 HP.", "score": 8},
                       {"type": "CURIOSITY", "text": "Something is off about the third player.", "score": 7.8}]}
    _fake_llm(monkeypatch, [json.dumps(hooks)])
    assert hook_agent.generate_hooks(MOMENT, SUMMARY, recent_hook_types=["SHOCK"])["type"] == "CURIOSITY"


# ── commentary_agent ────────────────────────────────────────────────────────

def test_style_by_moment_and_word_range():
    assert commentary_agent.style_for({"moment_type": "FAIL"}) == "funny"
    assert commentary_agent.style_for({"moment_type": "INSANE_PLAY"}) == "analysis"
    assert commentary_agent.style_for(None) == "hype"
    lo, hi = commentary_agent.target_word_range(25)
    assert (lo, hi) == (48, 69)
    assert commentary_agent.target_word_range(2)[0] >= 28 and commentary_agent.target_word_range(300)[1] <= 98


GOOD = ("He survived with 1 HP.\nThree players were pushing him and he was already out of room.\n...\n"
        "Look at that health bar and tell me he should still be alive here.\nSomehow he turns the whole "
        "fight around and walks away with it.")


def test_commentary_regenerates_on_local_issues(monkeypatch):
    bad = "Watch what happens. This insane moment gave him 9 kills, absolutely incredible."
    calls = _fake_llm(monkeypatch, [bad, GOOD])
    script, issues = commentary_agent.generate_commentary(MOMENT, {"text": "He survived with 1 HP.", "type": "SHOCK"},
                                                           SUMMARY, {"broadcaster": "x"}, duration=25)
    assert len(calls) == 2 and issues == []
    assert "numbers not in the clip data" in calls[1]          # feedback reached the retry prompt
    assert script.startswith("He survived with 1 HP.")


def test_commentary_prepends_hook_if_missing(monkeypatch):
    _fake_llm(monkeypatch, [GOOD.replace("He survived with 1 HP.\n", "")])
    script, _ = commentary_agent.generate_commentary(MOMENT, {"text": "How is he alive?", "type": "CURIOSITY"},
                                                     SUMMARY, {}, duration=25)
    assert script.startswith("How is he alive?\n")


# ── script_quality_agent ────────────────────────────────────────────────────

def _judge(**scores):
    base = {"hook": 8, "naturalness": 8, "originality": 8, "accuracy": 8, "moment_relevance": 8,
            "issues": [], "fix": ""}
    base.update(scores)
    return json.dumps(base)


def test_quality_passes_only_when_all_above_threshold(monkeypatch):
    _fake_llm(monkeypatch, [_judge()])
    assert script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)["passed"] is True
    _fake_llm(monkeypatch, [_judge(hook=5, fix="open harder")])      # hook has a hard floor of 6
    r = script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)
    assert r["passed"] is False and r["failing"] == ["hook"] and r["feedback"] == "open harder"


def test_balanced_rule_lets_a_good_but_not_perfect_script_pass(monkeypatch):
    _fake_llm(monkeypatch, [_judge(hook=6, naturalness=5, originality=5)])
    assert script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)["passed"] is True
    _fake_llm(monkeypatch, [_judge(accuracy=6)])                      # accuracy is a hard gate (7)
    assert script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)["failing"] == ["accuracy"]
    _fake_llm(monkeypatch, [_judge(naturalness=4)])                   # floor: must be > 4
    assert script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)["failing"] == ["naturalness"]
    _fake_llm(monkeypatch, [_judge(hook=6, naturalness=5, originality=5, accuracy=7, moment_relevance=5)])
    assert script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)["passed"] is False


def test_strict_rule_still_available(monkeypatch):
    monkeypatch.setattr(script_quality_agent, "RULE", "strict")
    _fake_llm(monkeypatch, [_judge(hook=7)])
    assert script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)["passed"] is False


def test_quality_caps_invented_numbers_and_ai_phrases_and_duplicates(monkeypatch):
    _fake_llm(monkeypatch, [_judge()])                                 # lenient judge
    r = script_quality_agent.evaluate_script("He got 9 kills.", MOMENT, None, SUMMARY)
    assert r["passed"] is False and "accuracy" in r["failing"]
    r = script_quality_agent.evaluate_script("Watch what happens when he clutches it with 1 HP.", MOMENT, None,
                                             SUMMARY, recent_scripts=["watch what happens ok"])
    assert "naturalness" in r["failing"]
    r = script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY, recent_scripts=[GOOD])
    assert "originality" in r["failing"]


def test_quality_judge_outage_fails_closed_unless_opted_out(monkeypatch):
    _fake_llm(monkeypatch, [RuntimeError("down")])
    r = script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)
    assert r["passed"] is False and r["unjudged"] is True and r["judge_skipped"] is True
    assert "not reviewed" in r["feedback"]
    monkeypatch.setattr(script_quality_agent, "ALLOW_UNJUDGED", True)             # explicit opt-out
    assert script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)["passed"] is True
    bad = script_quality_agent.evaluate_script("He got 9 kills.", MOMENT, None, SUMMARY)
    assert bad["passed"] is False                                                  # deterministic checks still apply


def test_quality_unparsable_judge_reply_is_retried_once_then_unjudged(monkeypatch):
    calls = _fake_llm(monkeypatch, ["total nonsense", _judge()])
    assert script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)["passed"] is True
    assert len(calls) == 2
    calls = _fake_llm(monkeypatch, ["nonsense", "still nonsense"])
    r = script_quality_agent.evaluate_script(GOOD, MOMENT, None, SUMMARY)
    assert r["unjudged"] is True and r["passed"] is False and len(calls) == 2


# ── script_agent.create_gaming_script_v2 (orchestration) ────────────────────

class FakeDB:
    def __init__(self):
        self.meta = {}

    def get_meta(self, k):
        return self.meta.get(k)

    def set_meta(self, k, v):
        self.meta[k] = v


def test_v2_regenerates_with_feedback_then_passes(monkeypatch):
    hooks = json.dumps({"hooks": [{"type": "SHOCK", "text": "He survived with 1 HP.", "score": 9}]})
    state = {"judge": 0}

    def route(prompt):
        if "first 2 seconds" in prompt:
            return hooks
        if "strict editor" in prompt:
            state["judge"] += 1
            return _judge(hook=5, fix="sharper opening") if state["judge"] == 1 else _judge()
        return GOOD

    calls = _fake_llm(monkeypatch, [route])
    out = script_agent.create_gaming_script_v2({"duration": 25}, MOMENT, SUMMARY, {"broadcaster": "x"}, db=FakeDB())
    assert out["passed"] and out["attempts"] == 2 and not out["fallback"]
    assert any("sharper opening" in c for c in calls)          # judge feedback reached the rewrite


def test_v2_returns_not_passed_when_every_attempt_fails(monkeypatch):
    hooks = json.dumps({"hooks": [{"type": "SHOCK", "text": "He survived with 1 HP.", "score": 9}]})

    def route(prompt):
        if "first 2 seconds" in prompt:
            return hooks
        if "strict editor" in prompt:
            return _judge(originality=3)
        return GOOD

    _fake_llm(monkeypatch, [route])
    out = script_agent.create_gaming_script_v2({"duration": 25}, MOMENT, SUMMARY, {}, db=FakeDB())
    assert out["passed"] is False and out["attempts"] == script_agent.MAX_SCRIPT_ATTEMPTS


def test_v2_falls_back_to_legacy_when_commentary_breaks(monkeypatch):
    def route(prompt):
        if "first 2 seconds" in prompt:
            return "garbage"
        if "hype gaming commentator" in prompt:       # legacy prompt
            return " ".join(["word"] * 60)
        raise RuntimeError("commentary down")

    _fake_llm(monkeypatch, [route])
    out = script_agent.create_gaming_script_v2({"duration": 25}, MOMENT, SUMMARY, {}, db=FakeDB())
    assert out["fallback"] is True and out["passed"] is True and len(out["script"].split()) == 60


def test_recent_script_memory_is_bounded():
    db = FakeDB()
    for i in range(14):
        script_agent.remember_script(db, f"script {i}", "SHOCK")
    assert len(json.loads(db.meta[script_agent.RECENT_SCRIPTS_KEY])) == 10
    assert script_agent._load_recent(db, script_agent.RECENT_SCRIPTS_KEY, 5)[-1] == "script 13"


def test_legacy_prompt_no_longer_teaches_the_cliches(monkeypatch):
    calls = _fake_llm(monkeypatch, [" ".join(["w"] * 60)])
    script_agent.create_gaming_script("summary", {"broadcaster": "x", "game": "y"})
    assert "Nobody expected this..." not in calls[0]


# ── scheduler integration (select -> analyse -> script) ─────────────────────

@pytest.fixture
def sched(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "")
    import scheduler_gaming as s
    monkeypatch.setattr(s, "gaming_db", FakeDB())
    # Existing tests exercise the legacy renderer path; Sprint 2 tests opt in to "v2" explicitly.
    monkeypatch.setattr(s, "AUDIO_MODE", "legacy")
    monkeypatch.setattr(s, "NARRATION_MODE", "full")
    return s


def _wire(monkeypatch, s, clips, quality_pass=True, unjudged=False, non_gameplay=()):
    import agents_gaming.trending_agent as ta
    import agents_gaming.video_clip_agent as vca
    monkeypatch.setattr(ta, "FOLLOWED_STREAMERS", [])
    monkeypatch.setattr(vca, "download_twitch_clip",
                        lambda c, out: (open(out, "wb").write(b"x"), out)[1])
    seen = []

    def fake_analyze(c, p):
        seen.append(c["id"])
        boost = 10 if c["id"] == "best" else 3
        return {**MOMENT, "intensity": boost, "clutch_score": boost, "source": "vision",
                "is_gameplay": c["id"] not in non_gameplay}
    monkeypatch.setattr(moment_analyzer, "analyze_moment", fake_analyze)
    monkeypatch.setattr(script_agent, "create_gaming_script_v2",
                        lambda clip, m, sm, st, db=None, **kw: {"script": "S " * 40, "hook": {"type": "SHOCK", "text": "h"},
                                                          "passed": quality_pass and not unjudged, "quality": None,
                                                          "unjudged": unjudged})
    return seen


def test_scheduler_v2_analyses_top_k_and_picks_by_moment_not_views(sched, monkeypatch):
    filler = [_clip(f"low{i}", 100 * (i + 1), 6, title="boring") for i in range(9)]
    clips = [_clip("big", 90000, 6, title="boring"), _clip("mid", 80000, 6, title="boring"),
             _clip("best", 70000, 6, title="boring")] + filler
    seen = _wire(monkeypatch, sched, clips)
    res = sched._select_and_script_v2(clips, set())
    assert res["status"] == "ok"
    assert len(seen) == sched.ANALYZE_TOP_K and set(seen) == {"big", "mid", "best"}
    assert res["clip"]["id"] == "best"                        # lower views, far better moment
    assert os.path.exists(res["clip_path"])


def test_scheduler_v2_rejects_and_remembers_when_quality_fails(sched, monkeypatch):
    clips = [_clip(f"c{i}", 1000 - i, 3) for i in range(4)]
    _wire(monkeypatch, sched, clips, quality_pass=False)
    res = sched._select_and_script_v2(clips, set())
    assert res["status"] == "quality_rejected"
    assert len(sched._load_rejected()) == sched.MAX_CLIP_TRIES
    # next cycle skips the rejected clips instead of re-picking them
    res2 = sched._select_and_script_v2(clips, set())
    assert res2["status"] == "quality_rejected"
    assert len(sched._load_rejected()) == min(4, 2 * sched.MAX_CLIP_TRIES)


def test_scheduler_caption_text_strips_pause_markers(sched):
    out = sched._caption_text("He has 1 HP.\n...\nand wins\u2026 somehow")
    assert "..." not in out and "\u2026" not in out and "wins" in out


# ── full cycle smoke test (every external service faked) ────────────────────

def _fake_modules(monkeypatch, record):
    import sys
    import types

    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)

    mod("agents.voice_agent", generate_voice=lambda script, output_path: record.setdefault("voice", script))
    mod("agents.caption_agent", create_srt=lambda text, audio_path: record.setdefault("srt", text))
    mod("agents.video_agent", _create_video_from_pexels_clips=lambda paths, a, s, music_path=None:
        record.setdefault("render", paths) and "output/v.mp4")
    mod("agents_gaming.upload_agent", upload_video=lambda *a, **k: ("vid123", "https://youtu.be/vid123"))
    mod("agents_gaming.adaptive_scheduler", should_upload_now_gaming=lambda: (True, "ok"),
        mark_upload_done_gaming=lambda: record.setdefault("marked", True))
    mod("agents_gaming.seo_agent", generate_seo=lambda st, sc, use_pattern=None:
        (record.setdefault("seo_pattern", use_pattern) or "Title", "desc", ["#Gaming"]))


class _CycleDB(FakeDB):
    def get_all_posted_clip_ids(self):
        return set()

    def mark_posted(self, cid, title, who):
        self.meta["posted"] = cid

    def upsert_video(self, **kw):
        self.meta["video"] = kw


def test_full_cycle_v2_wiring(sched, monkeypatch):
    record = {}
    _fake_modules(monkeypatch, record)
    db = _CycleDB()
    monkeypatch.setattr(sched, "gaming_db", db)
    import agents_gaming.trending_agent as ta
    clips = [_clip(f"c{i}", 5000 - 100 * i, 3, title="1 HP clutch") for i in range(6)]
    monkeypatch.setattr(ta, "get_all_topics", lambda: clips)
    monkeypatch.setattr(ta, "FOLLOWED_STREAMERS", [])
    _wire(monkeypatch, sched, clips)
    monkeypatch.setattr(script_agent, "create_gaming_script_v2",
                        lambda clip, m, sm, st, db=None, **kw: {"script": "He had 1 HP.\n...\nand won", "passed": True,
                                                          "hook": {"type": "SHOCK", "text": "He had 1 HP."}, "quality": None})
    res = sched._run_gaming_cycle_inner()
    assert res["status"] == "uploaded" and res["video_url"].endswith("vid123")
    assert record["seo_pattern"] == "clutch"                 # moment type -> title pattern
    assert "..." in record["voice"] and "..." not in record["srt"]   # TTS keeps pauses, captions drop them
    assert record["render"][0].endswith(".mp4") and record["marked"]
    assert db.meta["posted"] and json.loads(db.meta[script_agent.RECENT_SCRIPTS_KEY]) == ["He had 1 HP.\n...\nand won"]
    assert json.loads(db.meta[script_agent.RECENT_HOOKS_KEY]) == ["SHOCK"]


def test_full_cycle_stops_without_upload_when_quality_rejected(sched, monkeypatch):
    record = {}
    _fake_modules(monkeypatch, record)
    monkeypatch.setattr(sched, "gaming_db", _CycleDB())
    import agents_gaming.trending_agent as ta
    clips = [_clip(f"c{i}", 5000 - 100 * i, 3) for i in range(5)]
    monkeypatch.setattr(ta, "get_all_topics", lambda: clips)
    monkeypatch.setattr(ta, "FOLLOWED_STREAMERS", [])
    _wire(monkeypatch, sched, clips, quality_pass=False)
    res = sched._run_gaming_cycle_inner()
    assert res["status"] == "quality_rejected" and "voice" not in record and "render" not in record


def test_full_cycle_legacy_flag_still_works(sched, monkeypatch):
    record = {}
    _fake_modules(monkeypatch, record)
    monkeypatch.setattr(sched, "gaming_db", _CycleDB())
    monkeypatch.setattr(sched, "GAMING_V2_ENABLED", False)
    import agents_gaming.trending_agent as ta
    import agents_gaming.video_clip_agent as vca
    clips = [_clip("c1", 5000, 3)]
    monkeypatch.setattr(ta, "get_all_topics", lambda: clips)
    monkeypatch.setattr(vca, "get_gaming_background_clip", lambda c: ["assets/gaming_clips/c1.mp4"])
    monkeypatch.setattr(script_agent, "create_gaming_script", lambda sm, st: "legacy script words here")
    res = sched._run_gaming_cycle_inner()
    assert res["status"] == "uploaded" and record["seo_pattern"] is None
    assert record["render"] == ["assets/gaming_clips/c1.mp4"]


# ── Sprint 1.1: fixes from the first live run ───────────────────────────────

HOOKS_JSON = json.dumps({"hooks": [{"type": "SHOCK", "text": "He survived with 1 HP.", "score": 9}]})
HOOKS2_JSON = json.dumps({"hooks": [{"type": "SHOCK", "text": "One HP left and three pushing him.", "score": 8}]})


def test_failing_hook_triggers_hook_regeneration_with_feedback_and_avoid_list(monkeypatch):
    state = {"hooks": 0, "judge": 0}
    prompts = []

    def route(prompt):
        if "first 2 seconds" in prompt:
            state["hooks"] += 1
            prompts.append(prompt)
            return HOOKS_JSON if state["hooks"] == 1 else HOOKS2_JSON
        if "strict editor" in prompt:
            state["judge"] += 1
            return _judge(hook=3, fix="open on the 1 HP detail") if state["judge"] == 1 else _judge()
        return GOOD

    _fake_llm(monkeypatch, [route])
    out = script_agent.create_gaming_script_v2({"duration": 25}, MOMENT, SUMMARY, {}, db=FakeDB())
    assert out["passed"] and state["hooks"] == 2
    assert "open on the 1 HP detail" in prompts[1] and "He survived with 1 HP." in prompts[1]
    assert out["hook"]["text"] == "One HP left and three pushing him."


def test_non_hook_failure_does_not_regenerate_hook(monkeypatch):
    state = {"hooks": 0, "judge": 0}

    def route(prompt):
        if "first 2 seconds" in prompt:
            state["hooks"] += 1
            return HOOKS_JSON
        if "strict editor" in prompt:
            state["judge"] += 1
            return _judge(naturalness=4) if state["judge"] == 1 else _judge()
        return GOOD

    _fake_llm(monkeypatch, [route])
    out = script_agent.create_gaming_script_v2({"duration": 25}, MOMENT, SUMMARY, {}, db=FakeDB())
    assert out["passed"] and state["hooks"] == 1


def test_hook_agent_excludes_avoided_hooks(monkeypatch):
    hooks = {"hooks": [{"type": "SHOCK", "text": "He survived with 1 HP.", "score": 9},
                       {"type": "STORY", "text": "Three players pushed him at 1 HP.", "score": 7}]}
    _fake_llm(monkeypatch, [json.dumps(hooks)])
    best = hook_agent.generate_hooks(MOMENT, SUMMARY, avoid=["he survived with 1 hp."])
    assert best["type"] == "STORY"


def _judge_env(monkeypatch, gemini_results):
    """gemini_results: list of (resp, err) tuples consumed per Gemini try; the router is tracked."""
    seen, timeouts = [], []
    monkeypatch.setattr(llm, "_provider_dead", lambda n: False)
    monkeypatch.setattr(llm, "_record_outcome", lambda *a: None)
    monkeypatch.setattr(llm, "_get_gemini", lambda *a, **k: SimpleNamespace(invoke=lambda p: None))
    monkeypatch.setattr(llm, "safe_invoke", lambda p, *a, **k: (seen.append("router"), SimpleNamespace(content="r"))[1])
    results = list(gemini_results)

    def fake_run(fn, t):
        timeouts.append(t)
        seen.append("gemini")
        return results.pop(0) if results else (None, "timed out")

    monkeypatch.setattr(llm, "_run_with_timeout", fake_run)
    monkeypatch.setattr(llm, "_normalize_response", lambda r: SimpleNamespace(content=r))
    return seen, timeouts


def test_judge_is_pinned_to_gemini_with_its_own_timeout(monkeypatch):
    seen, timeouts = _judge_env(monkeypatch, [("g", None)])
    assert script_quality_agent._invoke_judge("x").content == "g" and seen == ["gemini"]
    assert timeouts == [script_quality_agent.JUDGE_TIMEOUT_SECONDS] and timeouts[0] > 15   # not the router's 15s


def test_judge_retries_gemini_once_then_succeeds(monkeypatch):
    seen, _ = _judge_env(monkeypatch, [(None, "timed out"), ("g", None)])
    assert script_quality_agent._invoke_judge("x").content == "g"
    assert seen == ["gemini", "gemini"]                      # retried Gemini, never touched the router


def test_judge_does_not_fall_back_to_the_router_by_default(monkeypatch):
    seen, _ = _judge_env(monkeypatch, [(None, "timed out"), (None, "timed out")])
    with pytest.raises(RuntimeError):
        script_quality_agent._invoke_judge("x")
    assert "router" not in seen
    monkeypatch.setattr(llm, "_provider_dead", lambda n: True)                              # circuit open
    with pytest.raises(RuntimeError):
        script_quality_agent._invoke_judge("x")
    assert "router" not in seen


def test_unavailable_gemini_judge_makes_script_unjudged_not_rejected(monkeypatch):
    _judge_env(monkeypatch, [])                               # Gemini always times out
    q = script_quality_agent.evaluate_script("He survived with 1 HP.", MOMENT, None, SUMMARY)
    assert q["unjudged"] and not q["passed"]                  # scheduler skips the cycle, clip is not burned


def test_judge_router_fallback_is_opt_in(monkeypatch):
    seen, _ = _judge_env(monkeypatch, [(None, "timed out"), (None, "timed out")])
    monkeypatch.setattr(script_quality_agent, "JUDGE_ROUTER_FALLBACK", True)
    assert script_quality_agent._invoke_judge("x").content == "r" and seen[-1] == "router"
    seen.clear()
    monkeypatch.setattr(script_quality_agent, "JUDGE_PROVIDER", "router")
    script_quality_agent._invoke_judge("x")
    assert seen == ["router"]


def test_hook_question_is_penalised_and_stock_openers_rejected(monkeypatch):
    hooks = {"hooks": [
        {"type": "CURIOSITY", "text": "Can he hold the site with a rifle?", "score": 9},          # question: -2
        {"type": "STORY", "text": "Three players are pushing him.", "score": 8},
        {"type": "CHALLENGE", "text": "What happens when he runs out of room?", "score": 10},   # stock opener
        {"type": "REACTION", "text": "Can you keep up with this?", "score": 10},                 # stock opener
    ]}
    _fake_llm(monkeypatch, [json.dumps(hooks)])
    best = hook_agent.generate_hooks(MOMENT, SUMMARY)
    assert best["type"] == "STORY"
    assert [c["type"] for c in best["candidates"]] == ["STORY", "CURIOSITY"]


def test_hook_rejects_solo_claim_not_in_data(monkeypatch):
    hooks = {"hooks": [{"type": "STORY", "text": "He holds the site solo.", "score": 9},
                       {"type": "SHOCK", "text": "Three players are pushing him.", "score": 6}]}
    _fake_llm(monkeypatch, [json.dumps(hooks)])
    assert hook_agent.generate_hooks(MOMENT, SUMMARY)["type"] == "SHOCK"
    assert v2_utils.ungrounded_claims("He holds it solo", "he plays solo queue") == []      # allowed when in the data


def test_commentary_flags_stage_directions_and_invented_solo(monkeypatch):
    issues = commentary_agent._local_issues("Ace banner flashes, he smirks.\nHe holds B alone.",
                                            MOMENT, None, SUMMARY, [], 1, 50)
    assert any("describes the screen" in i for i in issues)
    assert any("claims not in the clip data" in i for i in issues)
    p = commentary_agent._build_prompt(MOMENT, None, SUMMARY, {}, "hype", 40, 60, None)
    assert "stage directions" in p and '"solo" or "alone"' in p
    assert '"solo" or "alone"' in commentary_agent._build_short_prompt(MOMENT, None, SUMMARY, "hype", 9, 20, None)


def test_pauses_off_by_default_and_opt_in(monkeypatch):
    moment = {**MOMENT}
    off = commentary_agent._build_prompt(moment, None, SUMMARY, {}, "hype", 40, 60, None)
    assert 'write "..."' not in off and "no dead air" in off
    monkeypatch.setattr(commentary_agent, "PAUSES_ENABLED", True)
    on = commentary_agent._build_prompt(moment, None, SUMMARY, {}, "hype", 40, 60, None)
    assert 'write "..."' in on and "no dead air" not in on


def test_hook_prompt_discourages_generic_questions_and_spoilers():
    p = hook_agent._build_prompt(MOMENT, SUMMARY, feedback="too vague", avoid=["old hook"])
    assert "at most ONE of the five may be a question" in p and "never repeat it in the hook" in p
    assert "too vague" in p and "old hook" in p


def test_prefer_languages():
    en, ja, none_ = _clip("e", 1, 1), _clip("j", 1, 1), _clip("n", 1, 1)
    en["language"], ja["language"] = "en", "ja"
    pool = [en, ja, none_, dict(_clip("e2", 1, 1), language="EN")]
    kept = clip_scorer.prefer_languages(pool, ["en"], min_keep=3)
    assert {c["id"] for c in kept} == {"e", "n", "e2"}                        # untagged kept, case-insensitive
    assert clip_scorer.prefer_languages(pool, ["en"], min_keep=4) is pool     # too few -> fall back to everything
    assert clip_scorer.prefer_languages(pool, [], min_keep=1) is pool         # no preference


def test_scheduler_skips_non_english_when_enough_english(sched, monkeypatch):
    clips = [dict(_clip(f"en{i}", 1000 - i * 10, 3), language="en") for i in range(5)]
    clips += [dict(_clip(f"ja{i}", 90000, 3), language="ja") for i in range(3)]      # far more views, wrong language
    seen = _wire(monkeypatch, sched, clips)
    monkeypatch.setattr(sched, "CLIP_LANGUAGES", ["en"])
    res = sched._select_and_script_v2(clips, set())
    assert res["status"] == "ok" and all(i.startswith("en") for i in seen) and res["clip"]["id"].startswith("en")


# ── Sprint 1.2: fixes from the second live run ──────────────────────────────

def test_orchestrator_stops_immediately_when_unjudged(monkeypatch):
    state = {"hooks": 0, "judge": 0}

    def route(prompt):
        if "first 2 seconds" in prompt:
            state["hooks"] += 1
            return HOOKS_JSON
        if "strict editor" in prompt:
            state["judge"] += 1
            raise RuntimeError("judge down")
        return GOOD

    _fake_llm(monkeypatch, [route])
    out = script_agent.create_gaming_script_v2({"duration": 25}, MOMENT, SUMMARY, {}, db=FakeDB())
    assert out["passed"] is False and out["unjudged"] is True and out["attempts"] == 1
    assert state["hooks"] == 1                                  # no pointless hook regeneration / retries


def test_scheduler_judge_outage_skips_cycle_without_burning_the_clip(sched, monkeypatch):
    clips = [_clip(f"c{i}", 1000 - i, 3) for i in range(5)]
    _wire(monkeypatch, sched, clips, unjudged=True)
    res = sched._select_and_script_v2(clips, set())
    assert res == {"status": "judge_unavailable"}
    assert sched._load_rejected() == set()                      # clip is NOT marked rejected


def test_full_cycle_does_not_upload_when_judge_unavailable(sched, monkeypatch):
    record = {}
    _fake_modules(monkeypatch, record)
    monkeypatch.setattr(sched, "gaming_db", _CycleDB())
    import agents_gaming.trending_agent as ta
    clips = [_clip(f"c{i}", 5000 - 100 * i, 3) for i in range(5)]
    monkeypatch.setattr(ta, "get_all_topics", lambda: clips)
    monkeypatch.setattr(ta, "FOLLOWED_STREAMERS", [])
    _wire(monkeypatch, sched, clips, unjudged=True)
    res = sched._run_gaming_cycle_inner()
    assert res["status"] == "judge_unavailable" and "voice" not in record and "render" not in record


def test_non_gameplay_clips_are_dropped_and_remembered(sched, monkeypatch):
    filler = [_clip(f"low{i}", 100 * (i + 1), 6, title="boring") for i in range(9)]
    clips = [_clip("irl", 90000, 6), _clip("game1", 80000, 6), _clip("game2", 70000, 6)] + filler
    seen = _wire(monkeypatch, sched, clips, non_gameplay={"irl"})
    res = sched._select_and_script_v2(clips, set())
    assert res["status"] == "ok" and res["clip"]["id"] != "irl"
    assert "irl" in seen and "irl" in sched._load_rejected()


def test_normalize_moment_is_gameplay_parsing():
    base = {"moment_type": "FAIL", "what_happened": "x"}
    assert moment_analyzer.normalize_moment({**base, "is_gameplay": False}, {})["is_gameplay"] is False
    assert moment_analyzer.normalize_moment({**base, "is_gameplay": "false"}, {})["is_gameplay"] is False
    assert moment_analyzer.normalize_moment({**base, "is_gameplay": "true"}, {})["is_gameplay"] is True
    assert moment_analyzer.normalize_moment(base, {})["is_gameplay"] is True            # missing -> assume gameplay
    assert moment_analyzer.heuristic_moment({"title": "x"})["is_gameplay"] is True


def test_full_cycle_gameplay_hook_reaches_renderer_and_keeps_text_hook_memory(sched, monkeypatch):
    """Hook Engine wiring: an intense, vision-analysed moment gets a gameplay cold open that is
    handed to the renderer and logged. Regression guard: the visual hook must not clobber the
    TEXT hook (`hook`) that feeds remember_script() / the hook-type cooldown."""
    import sys
    import types
    record = {}
    _fake_modules(monkeypatch, record)

    def render(paths, a, s, music_path=None, hook=None):
        record["render_hook"] = hook
        return "output/v.mp4"
    m = types.ModuleType("agents.video_agent")
    m._create_video_from_pexels_clips = render
    monkeypatch.setitem(sys.modules, "agents.video_agent", m)

    db = _CycleDB()
    monkeypatch.setattr(sched, "gaming_db", db)
    import agents_gaming.trending_agent as ta
    clips = [_clip("best", 5000, 3, title="1 HP clutch", dur=25)] + \
            [_clip(f"c{i}", 4000 - 100 * i, 3, title="1 HP clutch", dur=25) for i in range(5)]
    monkeypatch.setattr(ta, "get_all_topics", lambda: clips)
    monkeypatch.setattr(ta, "FOLLOWED_STREAMERS", [])
    _wire(monkeypatch, sched, clips)                       # "best" is analysed at intensity 10
    monkeypatch.setattr(script_agent, "create_gaming_script_v2",
                        lambda clip, mo, sm, st, db=None, **kw: {"script": "He had 1 HP.\n...\nand won", "passed": True,
                                                           "hook": {"type": "SHOCK", "text": "He had 1 HP."},
                                                           "quality": None})
    res = sched._run_gaming_cycle_inner()

    assert res["status"] == "uploaded"
    h = record["render_hook"]
    assert h and h["provider"] == "gameplay" and h["relevance"] == 1.0
    assert 0.8 <= h["duration"] <= 3.0 and h["start"] >= 0
    # the text-hook cooldown memory is intact (this failed when both used the name `hook`)
    assert json.loads(db.meta[script_agent.RECENT_HOOKS_KEY]) == ["SHOCK"]
    # selection was logged against the uploaded video
    log = json.loads(db.meta["hook_history:gaming"])["log"]
    assert log[-1]["video_id"] == "vid123" and log[-1]["hook_provider"] == "gameplay"


def test_loop_continues_past_non_gameplay_clips_until_enough_usable_ones(sched, monkeypatch):
    filler = [_clip(f"low{i}", 100 * (i + 1), 6, title="boring") for i in range(9)]
    clips = [_clip("irl1", 90000, 6), _clip("irl2", 85000, 6), _clip("g1", 80000, 6),
             _clip("g2", 75000, 6), _clip("g3", 70000, 6)] + filler
    seen = _wire(monkeypatch, sched, clips, non_gameplay={"irl1", "irl2"})
    monkeypatch.setattr(sched, "ANALYZE_TOP_K", 2)
    monkeypatch.setattr(sched, "MAX_ANALYZED", 5)
    res = sched._select_and_script_v2(clips, set())
    assert res["status"] == "ok" and res["clip"]["id"] in {"g1", "g2"}
    assert seen == ["irl1", "irl2", "g1", "g2"]                 # kept looking, stopped at 2 usable (g3 untouched)
    assert sched._load_rejected() == {"irl1", "irl2"}


def test_analysis_loop_respects_the_cap_and_reports_why_nothing_was_usable(sched, monkeypatch, capsys):
    clips = [_clip(f"irl{i}", 90000 - i * 100, 6) for i in range(8)]
    seen = _wire(monkeypatch, sched, clips, non_gameplay={c["id"] for c in clips})
    monkeypatch.setattr(sched, "ANALYZE_TOP_K", 2)
    monkeypatch.setattr(sched, "MAX_ANALYZED", 3)
    res = sched._select_and_script_v2(clips, set())
    assert res == {"status": "no_new_clip", "reason": "every analysed clip was non-gameplay footage"}
    assert len(seen) == 3                                        # hard cap on vision calls
    out = capsys.readouterr().out
    assert "is not gameplay footage (game=" in out and "what=" in out    # the log says WHY, so it can be judged


def test_best_attempt_is_returned_and_early_stop(monkeypatch):
    hooks = json.dumps({"hooks": [{"type": "SHOCK", "text": "He survived with 1 HP.", "score": 9}]})
    seq = iter([_judge(hook=5, originality=5), _judge(hook=3, naturalness=3, originality=2, accuracy=9),
                _judge(hook=5, originality=5)])

    def route(prompt):
        if "first 2 seconds" in prompt:
            return hooks
        if "strict editor" in prompt:
            return next(seq)
        return GOOD

    _fake_llm(monkeypatch, [route])
    out = script_agent.create_gaming_script_v2({"duration": 25}, MOMENT, SUMMARY, {}, db=FakeDB())
    assert out["passed"] is False
    assert out["attempts"] == 1 or out["quality"]["scores"]["hook"] == 5   # the better draft, not the last
