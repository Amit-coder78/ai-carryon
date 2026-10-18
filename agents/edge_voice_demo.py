"""Preview Hindi edge-tts voices so you can pick one by ear.

    python agents/edge_voice_demo.py                # render all presets -> output/voice_samples/
    python agents/edge_voice_demo.py --list         # show every hi-IN voice Edge offers right now
    python agents/edge_voice_demo.py --experimental # also try multilingual voices (may sound off or fail)
    python agents/edge_voice_demo.py --text "..."   # use your own sentence

Then set the winner as GitHub repo *variables* (Settings -> Secrets and variables ->
Actions -> Variables), e.g. for the Hindi channel:
    HINDI_EDGE_VOICE = hi-IN-SwaraNeural
    HINDI_EDGE_RATE  = +10%
    HINDI_EDGE_PITCH = +3Hz
(Cricket uses the CRICKET_EDGE_* names.) Empty/unset = hi-IN-MadhurNeural, no change.
"""
import argparse
import asyncio
import os

SAMPLE_TEXT = (
    "क्या आपने देखा? कल रात के मैच में आख़िरी ओवर में छक्का लगा और पूरा स्टेडियम झूम उठा। "
    "ये जीत भारतीय क्रिकेट के लिए बहुत बड़ी है।"
)

# (label, voice, rate, pitch) — Edge only ships two hi-IN voices (Madhur, Swara),
# so the other options are tuned variants of those two.
PRESETS = [
    ("1_madhur_default",   "hi-IN-MadhurNeural", "+0%",  "+0Hz"),   # what you have now
    ("2_madhur_energetic", "hi-IN-MadhurNeural", "+12%", "+2Hz"),   # punchy, sports/news
    ("3_madhur_deep",      "hi-IN-MadhurNeural", "+5%",  "-8Hz"),   # deeper, documentary feel
    ("4_swara_default",    "hi-IN-SwaraNeural",  "+0%",  "+0Hz"),   # female, neutral
    ("5_swara_bright",     "hi-IN-SwaraNeural",  "+10%", "+3Hz"),   # female, upbeat
]

EXPERIMENTAL = [
    ("x1_ava_multilingual",    "en-US-AvaMultilingualNeural",    "+5%", "+0Hz"),
    ("x2_andrew_multilingual", "en-US-AndrewMultilingualNeural", "+5%", "+0Hz"),
    ("x3_brian_multilingual",  "en-US-BrianMultilingualNeural",  "+5%", "+0Hz"),
    ("x4_emma_multilingual",   "en-US-EmmaMultilingualNeural",   "+5%", "+0Hz"),
]


async def _list_hindi():
    import edge_tts
    voices = await edge_tts.list_voices()
    for v in voices:
        if v["Locale"] == "hi-IN":
            print(f'{v["ShortName"]:24} {v["Gender"]}')


async def _render(label, voice, rate, pitch, text, out_dir):
    """Returns True on success. A voice can be unavailable or reject Hindi text."""
    import edge_tts
    path = os.path.join(out_dir, f"{label}.mp3")
    try:
        await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(path)
        print(f"ok    {label:24} {voice}  rate={rate} pitch={pitch}  -> {path}")
        return True
    except Exception as e:
        print(f"FAIL  {label:24} {voice}: {str(e)[:120]}")
        return False


async def _main(args):
    if args.list:
        await _list_hindi()
        return
    out_dir = "output/voice_samples"
    os.makedirs(out_dir, exist_ok=True)
    import importlib.metadata as md
    print(f"edge-tts {md.version('edge-tts')}")
    presets = PRESETS + (EXPERIMENTAL if args.experimental else [])
    ok = 0
    for label, voice, rate, pitch in presets:
        ok += await _render(label, voice, rate, pitch, args.text, out_dir)
    print(f"\n{ok}/{len(presets)} voices rendered.")
    if ok == 0:
        raise SystemExit(1)  # every voice failed (e.g. HTTP 403 from Microsoft)
    print("Listen to the files, then set <CHANNEL>_EDGE_VOICE / _RATE / _PITCH as repo variables.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--experimental", action="store_true")
    ap.add_argument("--text", default=SAMPLE_TEXT)
    asyncio.run(_main(ap.parse_args()))
