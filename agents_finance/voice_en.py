"""English voice for the Finance channel (Edge TTS, no API key needed).
Defaults are a touch faster and lower for a confident creator feel; override with FINANCE_EDGE_RATE / FINANCE_EDGE_PITCH."""
import asyncio
import os

VOICE = os.environ.get("FINANCE_EDGE_VOICE") or "en-IN-PrabhatNeural"
RATE = os.environ.get("FINANCE_EDGE_RATE") or "+8%"
PITCH = os.environ.get("FINANCE_EDGE_PITCH") or "-2Hz"


async def _save(text, output_path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH).save(output_path)


def generate_voice(text, output_path="output/voice.mp3"):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    asyncio.run(_save(text, output_path))
    print(f"Finance voice generated (Edge/{VOICE}): {output_path}")
    return output_path
