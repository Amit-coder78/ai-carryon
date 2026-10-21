"""Gaming V2 Sprint 2 tests. LLM / Whisper / TTS / Twitch are faked; ffmpeg is REAL
(the renderer's claims — ducking, zoom, freeze, cold open, durations — are measured
on rendered files, not assumed)."""
import json
import os
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from agents_gaming import (audio_peak, commentary_agent, editor_agent, producer,
                           script_quality_agent, transcript_agent, video_renderer, voice_agent)

FFMPEG = shutil.which("ffmpeg")


@pytest.fixture(autouse=True)
def _fast_encode(monkeypatch):
    monkeypatch.setattr(video_renderer, "X264_PRESET", "ultrafast")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def _has_filter(name):
    if not FFMPEG:
        return False
    out = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return re.search(rf"\s{name}\s", out) is not None


# Some ffmpeg builds (e.g. certain Homebrew ones) ship without libass, so the 'ass' caption filter is missing.
# The renderer then deliberately falls back to a caption-less video; this test needs real captions.
needs_libass = pytest.mark.skipif(not _has_filter("ass"), reason="this ffmpeg build has no 'ass' filter (libass)")


# ── media fixtures (generated once) ─────────────────────────────────────────

def _ff(*args):
    subprocess.run([FFMPEG, "-y", "-v", "error", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    audio = ("sine=frequency=440:duration=12:sample_rate=48000",)
    burst = "[1:a]volume=0.2,volume='if(between(t,7.0,7.5),4,1)':eval=frame[a]"
    out = {}
    for name, src in (("moving", "testsrc2=size=1920x1080:rate=30:duration=12"),
                      ("static", "smptebars=size=1920x1080:rate=30:duration=12")):
        path = str(d / f"{name}.mp4")
        _ff("-f", "lavfi", "-i", src, "-f", "lavfi", "-i", audio[0], "-filter_complex", burst,
            "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", path)
        out[name] = path
    out["noaudio"] = str(d / "noaudio.mp4")
    _ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=6", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", out["noaudio"])
    for name, dur in (("hook", 1.6), ("setup", 2.2), ("tag", 1.1)):
        path = str(d / f"{name}.mp3")
        _ff("-f", "lavfi", "-i", f"sine=frequency=4000:duration={dur}:sample_rate=24000", "-ac", "1",
            "-c:a", "libmp3lame", path)
        out[name] = path
    out["dir"] = str(d)
    return out


def _level(path, ss, t, lowpass=True):
    """Mean level (dB) of the game tone (440 Hz) in a window; narration is a 4 kHz tone, removed by low-pass."""
    af = ("lowpass=f=700,lowpass=f=700," if lowpass else "") + "volumedetect"
    r = subprocess.run([FFMPEG, "-v", "info", "-ss", str(ss), "-t", str(t), "-i", path, "-vn", "-af", af,
                        "-f", "null", "-"], capture_output=True, text=True)
    return float(re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr).group(1))


def _frame(path, t, out):
    _ff("-ss", str(t), "-i", path, "-frames:v", "1", out)
    from PIL import Image
    return Image.open(out).convert("RGB")


def _panel_diff(a, b):
    """Mean abs pixel difference of the gameplay panel (the middle band) of two 1080x1920 frames."""
    from PIL import ImageChops, ImageStat
    box = (0, 700, 1080, 1220)
    return sum(ImageStat.Stat(ImageChops.difference(a.crop(box), b.crop(box))).mean) / 3


def _line(media, kind, text, dur):
    return {"kind": kind, "text": text, "path": media[kind], "duration": dur}


# ── audio_peak ──────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_refine_key_snaps_to_the_loudest_burst(media):
    key, changed = audio_peak.refine_key_timestamp(media["moving"], 6.2)     # burst is at 7.0-7.5
    assert changed and 6.9 <= key <= 7.6


@needs_ffmpeg
def test_refine_key_leaves_it_alone_when_nothing_stands_out(media):
    assert audio_peak.refine_key_timestamp(media["moving"], 2.0) == (2.0, False)     # burst outside the radius
    assert audio_peak.refine_key_timestamp(media["noaudio"], 3.0) == (3.0, False)    # no audio at all
    assert audio_peak.refine_key_timestamp("/nonexistent.mp4", 3.0) == (3.0, False)
    assert audio_peak.refine_key_timestamp(media["moving"], None) == (None, False)


@needs_ffmpeg
def test_has_audio_stream(media):
    assert audio_peak.has_audio_stream(media["moving"]) and not audio_peak.has_audio_stream(media["noaudio"])


# ── transcript_agent ────────────────────────────────────────────────────────

def test_clean_segments_filters_non_speech_and_hallucinations():
    segs = [
        {"start": 0, "end": 2, "text": " No way he hit that ", "no_speech_prob": 0.05, "avg_logprob": -0.2},
        {"start": 2, "end": 4, "text": "Thanks for watching!", "no_speech_prob": 0.1, "avg_logprob": -0.2},
        {"start": 4, "end": 6, "text": "mumble", "no_speech_prob": 0.9, "avg_logprob": -0.2},
        {"start": 6, "end": 8, "text": "garbled", "no_speech_prob": 0.1, "avg_logprob": -2.5},
        {"start": 8, "end": 9, "text": "", "no_speech_prob": 0.0, "avg_logprob": 0.0},
        "junk",
    ]
    assert transcript_agent.clean_segments(segs) == [{"start": 0.0, "end": 2.0, "text": "No way he hit that"}]


class FakeGroq:
    def __init__(self, resp=None, boom=None):
        self.calls, self._resp, self._boom = [], resp, boom
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self._boom:
            raise self._boom
        return self._resp


GOOD_RESP = {"language": "en", "segments": [
    {"start": 1.0, "end": 3.5, "text": "Oh my god he actually did it", "no_speech_prob": 0.02, "avg_logprob": -0.1}]}


@needs_ffmpeg
def test_transcribe_clip_happy_path_and_request_shape(media):
    client = FakeGroq(GOOD_RESP)
    out = transcript_agent.transcribe_clip(media["moving"], client=client)
    assert out["text"] == "Oh my god he actually did it" and out["language"] == "en" and out["words"] == 7
    call = client.calls[0]
    assert call["model"] == transcript_agent.WHISPER_MODEL and call["response_format"] == "verbose_json"
    assert call["file"][0].endswith(".flac") and len(call["file"][1]) > 0          # real extracted audio was sent


@needs_ffmpeg
def test_transcribe_clip_degrades_to_none(media, monkeypatch):
    assert transcript_agent.transcribe_clip(media["noaudio"], client=FakeGroq(GOOD_RESP)) is None     # no audio
    assert transcript_agent.transcribe_clip(media["moving"], client=FakeGroq(boom=RuntimeError("429"))) is None
    few = {"language": "en", "segments": [{"start": 0, "end": 1, "text": "uh yeah", "no_speech_prob": 0,
                                           "avg_logprob": 0}]}
    assert transcript_agent.transcribe_clip(media["moving"], client=FakeGroq(few)) is None              # < 3 words
    monkeypatch.setattr(transcript_agent, "TRANSCRIBE_ENABLED", False)
    assert transcript_agent.transcribe_clip(media["moving"], client=FakeGroq(GOOD_RESP)) is None
    assert transcript_agent.transcribe_clip("/nonexistent.mp4") is None


def test_speech_share():
    tr = {"segments": [{"start": 0, "end": 2}, {"start": 5, "end": 6}]}
    assert transcript_agent.speech_share(tr, 12) == pytest.approx(0.25)
    assert transcript_agent.speech_share(None, 12) == 0.0 and transcript_agent.speech_share(tr, 0) == 0.0


# ── voice_agent ─────────────────────────────────────────────────────────────

def test_voice_style_follows_the_moment():
    assert voice_agent.voice_style_for({"moment_type": "CLUTCH"}) == "dramatic"
    assert voice_agent.voice_style_for({"moment_type": "FAIL"}) == "funny"
    assert voice_agent.voice_style_for({"moment_type": "RAGE"}) == "rage"
    assert voice_agent.voice_style_for(None) == "hype"
    assert set(voice_agent.VOICE_STYLE_BY_MOMENT.values()) <= set(voice_agent.VOICE_PROFILES)
    assert all(p["rate"].startswith(("+", "-")) for p in voice_agent.VOICE_PROFILES.values())


@needs_ffmpeg
def test_synth_line_falls_back_voice_then_shared_then_none(media, tmp_path, monkeypatch):
    tried = []

    async def fake_edge(text, path, voice, rate, pitch):
        tried.append(voice)
        if voice == "en-US-GuyNeural":
            raise RuntimeError("voice unavailable")
        shutil.copy(media["hook"], path)

    monkeypatch.setattr(voice_agent, "_edge_save", fake_edge)
    dur = voice_agent.synth_line("He had one HP... left", str(tmp_path / "a.mp3"), "hype")
    assert tried == ["en-US-GuyNeural", voice_agent.FALLBACK_VOICE] and dur == pytest.approx(1.6, abs=0.15)

    async def always_fail(*a, **k):
        raise RuntimeError("edge down")
    monkeypatch.setattr(voice_agent, "_edge_save", always_fail)
    monkeypatch.setattr(voice_agent, "_tts_shared", lambda text, path: shutil.copy(media["tag"], path) and True)
    assert voice_agent.synth_line("hello there", str(tmp_path / "b.mp3")) == pytest.approx(1.1, abs=0.15)
    monkeypatch.setattr(voice_agent, "_tts_shared", lambda text, path: False)
    assert voice_agent.synth_line("hello there", str(tmp_path / "c.mp3")) is None
    assert voice_agent.synth_line("   ", str(tmp_path / "d.mp3")) is None


@needs_ffmpeg
def test_synth_lines_skips_lines_that_fail(media, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_agent, "synth_line",
                        lambda text, path, style: None if "bad" in text else 1.5)
    out = voice_agent.synth_lines([{"kind": "hook", "text": "good"}, {"kind": "setup", "text": "bad one"}],
                                  "hype", str(tmp_path))
    assert [o["kind"] for o in out] == ["hook"] and out[0]["duration"] == 1.5


# ── editor_agent ────────────────────────────────────────────────────────────

def _moment(**kw):
    base = {"moment_type": "CLUTCH", "intensity": 9, "key_timestamp": 7.0, "source": "vision"}
    return {**base, **kw}


def test_plan_edit_effects_only_when_the_payoff_time_is_trustworthy():
    p = editor_agent.plan_edit(_moment(), 12.0)
    assert p["key"] == 7.0 and p["zoom"] and p["freeze"]["duration"] == 0.15 and 0.06 <= p["zoom"]["amount"] <= 0.14
    assert p["zoom"]["start"] < p["zoom"]["peak"] < p["key"] < p["zoom"]["hold_until"] < p["zoom"]["end"]
    for bad in (_moment(source="text"), _moment(source="heuristic")):
        q = editor_agent.plan_edit(bad, 12.0)
        assert q["zoom"] is None and q["freeze"] is None and q["key"] is None      # no trust, no effects
    assert editor_agent.plan_edit(_moment(key_timestamp=0.5), 12.0)["zoom"] is None   # too early
    assert editor_agent.plan_edit(_moment(key_timestamp=11.8), 12.0)["zoom"] is None  # too close to the end
    assert editor_agent.plan_edit(_moment(key_timestamp=None), 12.0)["zoom"] is None
    assert editor_agent.plan_edit(None, 12.0)["zoom"] is None


def test_plan_edit_scales_with_intensity():
    mid = editor_agent.plan_edit(_moment(intensity=7), 12.0)
    assert mid["zoom"] and mid["freeze"] is None                                    # freeze is for the biggest moments
    assert editor_agent.plan_edit(_moment(intensity=5), 12.0)["zoom"] is None       # not intense enough
    assert editor_agent.plan_edit(_moment(intensity=10, moment_type="DRAMA"), 12.0)["freeze"] is None
    hi, lo = editor_agent.plan_edit(_moment(intensity=10), 12.0), editor_agent.plan_edit(_moment(intensity=7), 12.0)
    assert hi["zoom"]["amount"] > lo["zoom"]["amount"]
    assert hi["freeze"]["duration"] <= 0.25


def _lines(h=1.6, s=2.2, t=1.1):
    return [{"kind": "hook", "text": "h", "duration": h}, {"kind": "setup", "text": "s", "duration": s},
            {"kind": "tag", "text": "t", "duration": t}]


def test_place_narration_orders_lines_around_the_payoff():
    placed, hold = editor_agent.place_narration(_lines(), 12.0, key=7.0, freeze_duration=0.17)
    by = {p["kind"]: p for p in placed}
    assert by["hook"]["start"] == editor_agent.HOOK_START
    assert by["setup"]["start"] >= by["hook"]["end"] + editor_agent.LINE_GAP - 1e-6
    assert by["setup"]["end"] <= 7.0 - editor_agent.PAYOFF_PRE                       # done before the payoff
    assert by["tag"]["start"] >= 7.0 + 0.17 + editor_agent.PAYOFF_POST - 1e-6        # lands after it
    assert hold == 0.0


def test_place_narration_never_talks_over_the_payoff_and_drops_what_does_not_fit():
    placed, _ = editor_agent.place_narration(_lines(), 12.0, key=2.0)                # payoff almost at the start
    assert [p["kind"] for p in placed] == ["tag"]                                     # hook/setup can't fit before it
    placed, _ = editor_agent.place_narration(_lines(s=9.0), 12.0, key=7.0)
    assert "setup" not in [p["kind"] for p in placed]                                 # dropped, never squeezed
    for p in editor_agent.place_narration(_lines(), 12.0, key=7.0)[0]:
        assert p["end"] <= 7.0 - editor_agent.PAYOFF_PRE or p["start"] >= 7.0 + editor_agent.PAYOFF_POST


def test_place_narration_end_hold_and_limits():
    placed, hold = editor_agent.place_narration(_lines(), 9.0, key=7.0)               # payoff near the end
    tag = [p for p in placed if p["kind"] == "tag"][0]
    assert 0 < hold <= editor_agent.END_HOLD_MAX and tag["end"] == pytest.approx(9.0 - 0.1 + hold, abs=0.02)
    placed, hold = editor_agent.place_narration(_lines(t=4.0), 9.0, key=7.0)          # would need a 3s+ hold
    assert "tag" not in [p["kind"] for p in placed] and hold == 0.0


def test_place_narration_without_a_trusted_key_uses_safe_fractions():
    placed, _ = editor_agent.place_narration(_lines(), 20.0, key=None)
    by = {p["kind"]: p for p in placed}
    assert by["hook"]["start"] == editor_agent.HOOK_START and by["setup"]["end"] <= 20.0 * 0.55
    assert by["tag"]["start"] >= 20.0 * 0.7 - 1e-6


def test_place_narration_prefers_gaps_in_the_streamers_speech():
    speech = [{"start": 0.0, "end": 3.0}]                                              # streamer talks over the opening
    placed, _ = editor_agent.place_narration([{"kind": "setup", "text": "s", "duration": 1.5}], 20.0,
                                             key=None, speech=speech)
    assert placed and placed[0]["start"] >= 3.0 - 1e-6                                 # waits for the gap
    solid = [{"start": 0.0, "end": 20.0}]
    assert editor_agent.place_narration([{"kind": "setup", "text": "s", "duration": 1.5}], 20.0,
                                        key=None, speech=solid)[0] == []               # nowhere to go: dropped


# ── captions (ASS) ──────────────────────────────────────────────────────────

def test_caption_chunks_are_short_timed_and_emphasised():
    chunks = video_renderer.caption_chunks("He had ONE HP left!", 10.0, 12.0)
    assert [len(c[2]) for c in chunks] == [3, 2] and chunks[0][0] == 10.0 and chunks[-1][1] == pytest.approx(12.0)
    assert chunks[0][1] == chunks[1][0]
    emph = {w: e for c in chunks for w, e in c[2]}
    assert emph["ONE"] and emph["HP"] and emph["LEFT!"] and not emph["HE"]
    assert video_renderer.caption_chunks("", 0, 1) == [] and video_renderer.caption_chunks("hi", 2, 1) == []


def test_out_time_maps_cold_open_and_freeze():
    co, fz = {"duration": 1.5}, {"at": 7.0, "duration": 0.2}
    assert video_renderer.out_time(3.0, co, fz) == 4.5            # before the freeze: just the cold-open shift
    assert video_renderer.out_time(8.0, co, fz) == pytest.approx(9.7)    # after: + freeze
    assert video_renderer.out_time(3.0, None, None) == 3.0


def test_build_ass_puts_hook_up_top_captions_low_and_in_output_time():
    placed = [{"kind": "hook", "text": "He had one HP", "start": 0.25, "end": 1.85},
              {"kind": "setup", "text": "Three players pushing him", "start": 2.3, "end": 4.5},
              {"kind": "tag", "text": "Ice cold", "start": 8.2, "end": 9.3}]
    ass = video_renderer.build_ass(placed, "He had one HP", {"duration": 1.5}, {"at": 7.0, "duration": 0.2},
                                   {"moment_type": "CLUTCH"}, "fit", 14.0)
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    hook = [l for l in lines if ",Hook," in l][0]
    assert "\\pos(540,330)" in hook and "HE HAD" in hook and hook.split(",")[1] == "0:00:00.00"
    assert hook.split(",")[2] == "0:00:03.65"                     # cold open 1.5 + hook end 1.85 + 0.3
    caps = [l for l in lines if ",Cap," in l]
    assert caps and all("\\pos(540,1420)" in l for l in caps) and caps[0].split(",")[1] == "0:00:03.80"
    tag = [l for l in lines if ",Reaction," in l]
    assert tag and tag[0].split(",")[1] == "0:00:09.90"           # 8.2 + 1.5 cold open + 0.2 freeze
    assert "&H0000D7FF&" in ass                                   # CLUTCH accent colour used for emphasis
    assert ass.count("[Events]") == 1 and "PlayResY: 1920" in ass


def test_ass_escapes_override_braces_in_words():
    ass = video_renderer.build_ass([{"kind": "setup", "text": "a {\\b1}bad word", "start": 1, "end": 2}], "", None,
                                   None, None, "fit", 5.0)
    assert "{\\b1}" not in ass.split("[Events]")[1].split("}", 1)[1] or "(" in ass


# ── renderer (real ffmpeg) ──────────────────────────────────────────────────

def _render(media, tmp_path, clip="moving", placed=None, plan=None, hook_text="", cold_open=None, end_hold=0.0,
            moment=None, layout="fit", name="out.mp4"):
    return video_renderer.render_gaming_video(
        media[clip], placed or [], plan or {}, hook_text, moment or {"moment_type": "CLUTCH"}, cold_open=cold_open,
        end_hold=end_hold, out_path=str(tmp_path / name), layout=layout, work_dir=str(tmp_path / "w"))


@needs_ffmpeg
def test_render_basic_geometry_streams_and_duration(media, tmp_path):
    res = _render(media, tmp_path)
    assert res["layout"] == "fit" and res["duration"] == pytest.approx(12.0, abs=0.15)
    assert video_renderer.probe_video_size(res["path"]) == (1080, 1920)
    assert audio_peak.has_audio_stream(res["path"])
    assert _level(res["path"], 3, 1) == pytest.approx(_level(media["moving"], 3, 1), abs=3.0)   # game audio is KEPT


@needs_ffmpeg
def test_render_ducks_game_audio_under_narration_and_recovers(media, tmp_path):
    placed = [{**_line(media, "hook", "He had one HP", 1.6), "start": 1.0, "end": 2.6}]
    res = _render(media, tmp_path, placed=placed, hook_text="He had one HP")
    under = _level(res["path"], 1.3, 1.0)          # while the narrator speaks
    clear = _level(res["path"], 4.0, 1.0)          # narrator silent
    after = _level(res["path"], 3.4, 0.4)          # shortly after the line ends: back near full
    assert clear - under >= 6.0                     # clearly ducked
    assert after - under >= 3.0 and clear - after <= 3.0
    narr = _level(res["path"], 1.3, 1.0, lowpass=False)
    assert narr > under                              # and the narration itself is audible on top
    # regression: the game audio must continue after the LAST narration line, right to the end of the video
    assert _level(res["path"], 6.0, 1.0) == pytest.approx(clear, abs=3.0)
    assert _level(res["path"], 10.8, 1.0) == pytest.approx(clear, abs=3.0)


@needs_ffmpeg
def test_render_punch_in_zooms_the_panel_then_eases_back(media, tmp_path):
    plan = {"key": 7.0, "zoom": {"start": 6.45, "peak": 6.85, "hold_until": 7.5, "end": 7.95, "amount": 0.12},
            "freeze": None}
    res = _render(media, tmp_path, clip="static", plan=plan)
    before = _frame(res["path"], 3.0, str(tmp_path / "f_before.png"))
    peak = _frame(res["path"], 7.2, str(tmp_path / "f_peak.png"))
    after = _frame(res["path"], 10.0, str(tmp_path / "f_after.png"))
    assert _panel_diff(before, peak) > 8.0           # the picture is visibly scaled at the payoff
    assert _panel_diff(before, after) < 1.0          # and eased fully back out afterwards
    assert res["duration"] == pytest.approx(12.0, abs=0.15)


@needs_ffmpeg
def test_render_freeze_frame_holds_the_picture_and_adds_its_duration(media, tmp_path):
    plan = {"key": 7.0, "zoom": None, "freeze": {"at": 7.0, "duration": 0.5}}
    res = _render(media, tmp_path, plan=plan)
    assert res["duration"] == pytest.approx(12.5, abs=0.15)
    a = _frame(res["path"], 7.1, str(tmp_path / "z1.png"))
    b = _frame(res["path"], 7.4, str(tmp_path / "z2.png"))
    c = _frame(res["path"], 6.5, str(tmp_path / "z3.png"))
    d = _frame(res["path"], 8.0, str(tmp_path / "z4.png"))
    assert _panel_diff(a, b) < 0.5                    # frozen: identical
    assert _panel_diff(c, a) > 2.0 and _panel_diff(b, d) > 2.0     # moving before and after
    assert _level(res["path"], 7.05, 0.35) < _level(res["path"], 5.0, 0.5) - 20   # game audio pauses with the picture


@needs_ffmpeg
def test_render_cold_open_prepends_the_peak_with_its_sound(media, tmp_path):
    cold = {"path": media["moving"], "start": 6.6, "duration": 1.5}                  # contains the 7.0-7.5 loud burst
    res = _render(media, tmp_path, cold_open=cold)
    assert res["duration"] == pytest.approx(13.5, abs=0.15)
    assert _level(res["path"], 0.5, 0.9) > _level(res["path"], 2.5, 0.9) + 6         # the burst is up front
    assert _level(res["path"], 8.5 + 1.5 - 1.5, 0.5) > _level(res["path"], 3.0, 0.5) + 6  # and still in the main clip


@needs_ffmpeg
def test_render_end_hold_extends_with_a_still_last_frame(media, tmp_path):
    res = _render(media, tmp_path, end_hold=1.0)
    assert res["duration"] == pytest.approx(13.0, abs=0.15)
    a = _frame(res["path"], 12.3, str(tmp_path / "h1.png"))
    b = _frame(res["path"], 12.9, str(tmp_path / "h2.png"))
    assert _panel_diff(a, b) < 0.5


@needs_ffmpeg
def test_render_handles_clips_without_audio_and_fill_layout(media, tmp_path):
    res = _render(media, tmp_path, clip="noaudio", placed=[{**_line(media, "hook", "h", 1.6), "start": 0.5, "end": 2.1}])
    assert res["duration"] == pytest.approx(6.0, abs=0.15) and audio_peak.has_audio_stream(res["path"])
    fill = _render(media, tmp_path, layout="fill", name="fill.mp4")
    assert fill["layout"] == "fill" and video_renderer.probe_video_size(fill["path"]) == (1080, 1920)


@needs_libass
def test_render_captions_sit_in_the_backdrop_bars_not_on_the_gameplay(media, tmp_path):
    placed = [{**_line(media, "setup", "Three players pushing him", 2.2), "start": 1.0, "end": 3.2}]
    res = _render(media, tmp_path, clip="static", placed=placed, hook_text="He had one HP")
    plain = _render(media, tmp_path, clip="static", name="plain.mp4")
    with_caps = _frame(res["path"], 2.0, str(tmp_path / "c1.png"))
    without = _frame(plain["path"], 2.0, str(tmp_path / "c2.png"))
    from PIL import ImageChops, ImageStat
    top, panel, bottom = (0, 150, 1080, 520), (0, 700, 1080, 1220), (0, 1300, 1080, 1560)
    delta = lambda box: sum(ImageStat.Stat(ImageChops.difference(with_caps.crop(box), without.crop(box))).mean)
    assert delta(top) > 5 and delta(bottom) > 5          # hook caption up top, narration caption below
    assert delta(panel) < 1                               # gameplay itself is untouched


@needs_ffmpeg
def test_render_retries_without_captions_when_libass_is_unavailable(media, tmp_path, monkeypatch):
    real = subprocess.run
    attempts = []

    def flaky(cmd, *a, **k):
        graph = cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""
        attempts.append("ass=" in graph)
        if "ass=" in graph:
            return subprocess.CompletedProcess(cmd, 1, "", "No such filter: 'ass'")
        return real(cmd, *a, **k)

    monkeypatch.setattr(video_renderer.subprocess, "run", flaky)
    res = _render(media, tmp_path, hook_text="Hook")
    assert res["duration"] == pytest.approx(12.0, abs=0.15) and True in attempts and False in attempts


@needs_ffmpeg
def test_render_raises_when_ffmpeg_fails_outright(media, tmp_path, monkeypatch):
    real = subprocess.run
    monkeypatch.setattr(video_renderer.subprocess, "run",
                        lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 1, "", "boom")
                        if "-filter_complex" in cmd else real(cmd, *a, **k))
    with pytest.raises(RuntimeError, match="render failed"):
        _render(media, tmp_path)


# ── producer ────────────────────────────────────────────────────────────────

def test_script_to_lines_shapes():
    s = producer.script_to_lines
    assert s("") == [] and s("...\n\n") == []
    assert s("One line") == [{"kind": "hook", "text": "One line"}]
    assert [l["kind"] for l in s("Hook\nSetup")] == ["hook", "setup"]
    three = s("He had one HP.\n...\nThree were pushing him.\nAnd more.\nIce cold.")
    assert [l["kind"] for l in three] == ["hook", "setup", "tag"]
    assert three[1]["text"] == "Three were pushing him. And more." and three[2]["text"] == "Ice cold."


@needs_ffmpeg
def test_produce_video_end_to_end_with_fake_tts(media, tmp_path, monkeypatch):
    def fake_synth(lines, style, out_dir):
        fake_synth.style = style
        return [{**l, "path": media[l["kind"]], "duration": {"hook": 1.6, "setup": 2.2, "tag": 1.1}[l["kind"]]}
                for l in lines]
    monkeypatch.setattr(producer, "synth_lines", fake_synth)
    moment = {"moment_type": "CLUTCH", "intensity": 9, "key_timestamp": 7.0, "source": "vision", "game": "Apex"}
    out = producer.produce_video(media["moving"], "He had one HP.\nThree were pushing him.\nIce cold.",
                                 {"text": "He had one HP", "type": "SHOCK"}, moment,
                                 transcript={"segments": [{"start": 9.0, "end": 11.0}]},
                                 cold_open={"path": media["moving"], "start": 6.3, "duration": 1.5},
                                 out_path=str(tmp_path / "final.mp4"), work_dir=str(tmp_path / "w"))
    assert fake_synth.style == "dramatic"
    assert video_renderer.probe_video_size(out) == (1080, 1920) and audio_peak.has_audio_stream(out)
    assert subprocess.run([FFMPEG, "-i", out], capture_output=True, text=True).stderr.count("Video:") == 1
    from agents_gaming.moment_analyzer import probe_duration
    # cold open 1.5 + clip 12.0 + freeze 0.15 + end hold 0.35 (the tag waits for the streamer's 9-11s speech
    # to finish, so it ends 0.35s after the clip does and the last frame is held for it)
    assert probe_duration(out) == pytest.approx(1.5 + 12.0 + 0.15 + 0.35, abs=0.2)


@needs_ffmpeg
def test_produce_video_survives_every_line_failing_to_voice(media, tmp_path, monkeypatch):
    monkeypatch.setattr(producer, "synth_lines", lambda lines, style, out_dir: [])
    out = producer.produce_video(media["moving"], "Hook line\nSetup line", None, None,
                                 out_path=str(tmp_path / "silent.mp4"), work_dir=str(tmp_path / "w"))
    assert audio_peak.has_audio_stream(out)                       # still a watchable clip with its own audio


# ── short-overlay commentary / judge ────────────────────────────────────────

def test_short_mode_budget_prompt_and_judge_note():
    lo, hi = commentary_agent.target_word_range(25, "short")
    assert (lo, hi) == (9, 22) and commentary_agent.target_word_range(60, "short")[1] == 30
    assert commentary_agent.target_word_range(25, "full") == commentary_agent.target_word_range(25)
    moment = {"game": "Apex", "moment_type": "CLUTCH", "what_happened": "w", "setup": "s", "payoff": "p",
              "reaction": "r"}
    p = commentary_agent._build_prompt(moment, {"text": "He had 1 HP.", "type": "SHOCK"}, "SUM", {}, "storytelling",
                                       lo, hi, None, "short")
    assert "SHORT voice-over" in p and "1. HOOK" in p and "2. SETUP" in p and "3. TAG" in p
    assert '"He had 1 HP."' in p and "KEEPS its own" in p and "streamer actually says" in p
    full = commentary_agent._build_prompt(moment, None, "SUM", {}, "hype", 40, 60, None)
    assert "SHORT voice-over" not in full
    assert "SHORT overlay" in script_quality_agent._judge_prompt("s", moment, None, "SUM", "short")
    assert "SHORT overlay" not in script_quality_agent._judge_prompt("s", moment, None, "SUM")


def test_moment_analyzer_prompt_includes_the_transcript():
    from agents_gaming import moment_analyzer
    block = moment_analyzer._metadata_block({"title": "t", "_transcript_text": "no way he hit that"})
    assert "Streamer speech" in block and "no way he hit that" in block
    assert "Streamer speech" not in moment_analyzer._metadata_block({"title": "t"})


# ── scheduler integration ───────────────────────────────────────────────────

from tests.test_gaming_v2 import (MOMENT, _clip, _CycleDB, _fake_modules, _wire, sched)  # noqa: E402,F401


def _v2(monkeypatch, sched, record, passed=True):
    import agents_gaming.script_agent as sa
    import agents_gaming.trending_agent as ta
    import agents_gaming.transcript_agent as tra
    monkeypatch.setattr(sched, "AUDIO_MODE", "v2")
    monkeypatch.setattr(sched, "NARRATION_MODE", "short")
    _fake_modules(monkeypatch, record)
    db = _CycleDB()
    monkeypatch.setattr(sched, "gaming_db", db)
    clips = [_clip(f"c{i}", 5000 - 100 * i, 3, title="1 HP clutch", dur=25) for i in range(6)]
    monkeypatch.setattr(ta, "get_all_topics", lambda: clips)
    monkeypatch.setattr(ta, "FOLLOWED_STREAMERS", [])
    _wire(monkeypatch, sched, clips)
    monkeypatch.setattr(tra, "transcribe_clip", lambda path: {"text": "oh my god he did it", "segments": []})

    def fake_script(clip, m, sm, st, db=None, mode=None, **kw):
        record["script_args"] = {"summary": sm, "mode": mode, "key": m["key_timestamp"]}
        return {"script": "He had 1 HP.\nThree pushed him.\nIce cold.", "passed": passed,
                "hook": {"type": "SHOCK", "text": "He had 1 HP."}, "quality": None}
    monkeypatch.setattr(sa, "create_gaming_script_v2", fake_script)
    return db


def test_scheduler_v2_audio_cycle_uses_transcript_short_mode_and_the_producer(sched, monkeypatch):
    import agents_gaming.audio_peak as ap
    import agents_gaming.producer as pr
    record = {}
    db = _v2(monkeypatch, sched, record)
    monkeypatch.setattr(ap, "refine_key_timestamp", lambda path, key: (9.9, True))
    import agents_gaming.gameplay_hook as gh
    monkeypatch.setattr(gh, "select_gameplay_hook", lambda path, moment, clip=None: {
        "provider": "gameplay", "path": path, "start": 9.0, "duration": 1.5, "hook_type": "shock"})

    def fake_produce(clip_path, script, hook, moment, transcript=None, cold_open=None, **kw):
        record["produce"] = {"script": script, "hook": hook, "transcript": transcript, "cold_open": cold_open,
                             "key": moment["key_timestamp"], "refined": moment.get("key_refined")}
        return "output/final.mp4"
    monkeypatch.setattr(pr, "produce_video", fake_produce)

    res = sched._run_gaming_cycle_inner()
    assert res["status"] == "uploaded"
    assert record["script_args"]["mode"] == "short"
    assert "oh my god he did it" in record["script_args"]["summary"]            # the writer HEARD the clip
    p = record["produce"]
    assert p["transcript"]["text"] == "oh my god he did it" and p["hook"]["text"] == "He had 1 HP."
    assert p["key"] == 9.9 and p["refined"] is True                              # audio-peak refined payoff
    assert p["cold_open"] and p["cold_open"]["provider"] == "gameplay"           # Hook Engine cold open handed over
    assert "voice" not in record and "srt" not in record                          # no legacy voice/SRT path
    assert json.loads(db.meta["gaming_recent_hook_types"]) == ["SHOCK"]


def test_scheduler_v2_render_failure_means_no_upload(sched, monkeypatch):
    import agents_gaming.producer as pr
    record = {}
    db = _v2(monkeypatch, sched, record)
    monkeypatch.setattr(pr, "produce_video", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffmpeg exploded")))
    res = sched._run_gaming_cycle_inner()
    assert res["status"] == "render_failed" and "exploded" in res["error"]
    assert "posted" not in db.meta and "marked" not in record                       # clip not marked posted; nothing uploaded


def test_scheduler_legacy_audio_mode_still_uses_the_shared_renderer(sched, monkeypatch):
    import agents_gaming.producer as pr
    record = {}
    _v2(monkeypatch, sched, record)
    monkeypatch.setattr(sched, "AUDIO_MODE", "legacy")
    monkeypatch.setattr(sched, "NARRATION_MODE", "full")
    monkeypatch.setattr(pr, "produce_video", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    res = sched._run_gaming_cycle_inner()
    assert res["status"] == "uploaded" and "voice" in record and record["render"]
    assert record["script_args"]["mode"] == "full"
