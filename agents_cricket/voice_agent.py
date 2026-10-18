# agents_cricket/voice_agent.py
"""Voice generation for the Cricket channel — Sarvam AI Bulbul v3, same
speaker (shubh) chosen for the Hindi channel, now targeting Hindi (hi-IN)
since cricket scripts are generated in Hindi.

Independent from agents/voice_agent.py (English edge-tts) and
agents_hindi/voice_agent.py (Hindi) on purpose — this channel needed its
own file so changing its voice never touches the other two channels.
"""
import os
import base64
from dotenv import load_dotenv

load_dotenv()  # load .env here too — don't rely on the caller having done it first

# Overridable per channel without a code change (GitHub repo *variables*):
#   CRICKET_SARVAM_SPEAKER / CRICKET_SARVAM_FALLBACK_SPEAKER
#   CRICKET_EDGE_VOICE / CRICKET_EDGE_RATE / CRICKET_EDGE_PITCH   (edge-tts fallback)
# Unset or empty -> the defaults below, i.e. the previous behaviour.
PRIMARY_SPEAKER = os.environ.get("CRICKET_SARVAM_SPEAKER") or "shubh"
FALLBACK_SPEAKER = os.environ.get("CRICKET_SARVAM_FALLBACK_SPEAKER") or "vijay"

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")


def _generate_sarvam(script, output_path, speaker):
    """Generate voice using Sarvam AI Bulbul v3 — Hindi (hi-IN).
    Sarvam splits long text into multiple audio chunks — concatenate ALL of them.
    Sarvam returns WAV — convert to MP3 to keep pipeline compatibility."""
    from sarvamai import SarvamAI
    from pydub import AudioSegment
    import io

    client = SarvamAI(api_subscription_key=SARVAM_API_KEY)

    text = script[:2500]

    audio = client.text_to_speech.convert(
        text=text,
        language_code="hi-IN",
        speaker=speaker,
        model="bulbul:v3",
        pace=1.15,
        temperature=0.7,
    )

    print(f"Sarvam returned {len(audio.audios)} audio segment(s)")

    combined = AudioSegment.empty()
    for i, audio_b64 in enumerate(audio.audios):
        wav_bytes = base64.b64decode(audio_b64)
        segment = AudioSegment.from_wav(io.BytesIO(wav_bytes))
        combined += segment
        print(f"  Segment {i+1}: {len(segment)/1000:.1f}s")

    combined.export(output_path, format="mp3")
    print(f"Total combined audio: {len(combined)/1000:.1f}s")


def _generate_edge_tts_fallback(script, output_path):
    """Fallback to edge-tts (Hindi) if Sarvam fails (e.g. quota exceeded)."""
    import asyncio
    import edge_tts

    async def _gen():
        voice = os.environ.get("CRICKET_EDGE_VOICE") or "hi-IN-MadhurNeural"
        rate = os.environ.get("CRICKET_EDGE_RATE") or "+0%"
        pitch = os.environ.get("CRICKET_EDGE_PITCH") or "+0Hz"
        print(f"edge-tts voice: {voice} rate={rate} pitch={pitch}")
        communicate = edge_tts.Communicate(script, voice, rate=rate, pitch=pitch)
        await communicate.save(output_path)

    asyncio.run(_gen())


def generate_voice(script, output_path="output/voice.mp3"):
    os.makedirs("output", exist_ok=True)

    if not SARVAM_API_KEY:
        print("SARVAM_API_KEY not set — using edge-tts fallback")
        _generate_edge_tts_fallback(script, output_path)
        return output_path

    try:
        _generate_sarvam(script, output_path, PRIMARY_SPEAKER)
        print(f"Cricket voice generated (Sarvam/{PRIMARY_SPEAKER}): {output_path}")
        return output_path
    except Exception as e:
        print(f"Primary speaker ({PRIMARY_SPEAKER}) failed: {e}")

    try:
        _generate_sarvam(script, output_path, FALLBACK_SPEAKER)
        print(f"Cricket voice generated (Sarvam/{FALLBACK_SPEAKER}): {output_path}")
        return output_path
    except Exception as e:
        print(f"Fallback speaker ({FALLBACK_SPEAKER}) failed: {e}")

    print("Sarvam AI unavailable — using edge-tts fallback")
    _generate_edge_tts_fallback(script, output_path)
    return output_path
