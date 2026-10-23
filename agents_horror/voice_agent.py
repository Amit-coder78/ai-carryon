# agents_horror/voice_agent.py
"""Stage 8 (narration). English edge-tts (already a dependency, already used by the English
channel). One clip per scene, so every scene's real length is known and video cuts are
synchronised to the narration. Pauses are inserted between scenes (longer before a reveal)."""
import os

DEFAULT_VOICES = ["en-US-AndrewMultilingualNeural", "en-US-GuyNeural", "en-GB-RyanNeural", "en-US-AvaMultilingualNeural"]
MAX_SHORTS_SEC = 59.0
MAX_SPEEDUP = 1.15


class VoiceError(RuntimeError):
    pass


def _settings():
    voices = ([os.environ["HORROR_EDGE_VOICE"]] if os.environ.get("HORROR_EDGE_VOICE") else []) + DEFAULT_VOICES
    return (voices, os.environ.get("HORROR_EDGE_RATE") or "-8%", os.environ.get("HORROR_EDGE_PITCH") or "-4Hz")


def _tts_edge(text, path, voice, rate, pitch):
    import asyncio
    import edge_tts

    async def _go():
        await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(path)
    asyncio.run(_go())


def _say(text, path, synth, voices, rate, pitch):
    last = None
    for v in voices:
        try:
            synth(text, path, v, rate, pitch)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return v
        except Exception as e:
            last = e
            print(f"[horror voice] {v} failed: {e}")
    raise VoiceError(f"all narration voices failed (last error: {last})")


def generate_narration(scenes, out_path="output/horror_voice.mp3", work_dir="output/horror_voice", synth=None,
                       max_total=None):
    """Returns {"path", "durations": [seconds per scene incl. pause], "total", "voice", "speedup"}.
    max_total: for Shorts, narration is sped up (<= x1.15) instead of being chopped when too long."""
    from pydub import AudioSegment

    voices, rate, pitch = _settings()
    synth = synth or _tts_edge
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    combined, durations, used = AudioSegment.empty(), [], None
    for sc in scenes:
        p = os.path.join(work_dir, f"{sc.scene_id}.mp3")
        used = _say(sc.narration, p, synth, voices, rate, pitch)
        seg = AudioSegment.from_file(p) + AudioSegment.silent(duration=int(sc.pause_after * 1000))
        durations.append(len(seg) / 1000.0)
        combined += seg
    combined.export(out_path, format="mp3")
    total, speedup = len(combined) / 1000.0, 1.0
    if max_total and total > max_total:
        speedup = min(total / (max_total - 0.2), MAX_SPEEDUP)
        _speed_up(out_path, speedup)
        durations = [d / speedup for d in durations]
        total = sum(durations)
        print(f"[horror voice] narration {total * speedup:.1f}s > {max_total}s: sped up x{speedup:.2f}")
    return {"path": out_path, "durations": durations, "total": total, "voice": used, "speedup": round(speedup, 3)}


def _speed_up(path, factor):
    import subprocess
    from agents.video_agent import get_ffmpeg
    tmp = path + ".tmp.mp3"
    subprocess.run([get_ffmpeg(), "-y", "-i", path, "-filter:a", f"atempo={factor:.4f}", tmp],
                   check=True, capture_output=True)
    os.replace(tmp, path)
