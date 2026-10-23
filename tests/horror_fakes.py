"""Shared mocks for the Horror tests: fake LLM, fake Pexels, fake TTS, fake Freesound.
No network, no API keys, no charges."""
import json
import os
import shutil
import subprocess
import types

from agents_horror.config import HorrorConfig

SCRIPT_SENTENCES = [
    "The light in the hostel corridor flickered twice, and Mira stopped walking.",
    "She was twenty-one, in a black hoodie and blue jeans, and the floor had been empty for a week.",
    "Room thirteen was supposed to be locked, yet a thin line of warm light slid under its door.",
    "She knocked softly, and something knocked back from the inside, slower than a heartbeat.",
    "The corridor window showed fog pressing against the glass, and the fog had hands.",
    "Mira backed away until her shoulder met the cold wall near the stairwell.",
    "The warm light went out, and every other bulb went with it.",
    "In the dark she heard her own voice whispering her name from room thirteen.",
    "The warden had said nobody lived there, and the warden had never lied before.",
    "She ran for the stairs, but the stairwell door opened onto the same flickering corridor.",
    "At the far end, room thirteen stood open, and the bed inside was made for her.",
    "On the pillow lay a black hoodie, still warm, and a note that said she was late.",
    "Mira looked down at her sleeves, and the hoodie she wore was already gone.",
]
SCRIPT = " ".join(SCRIPT_SENTENCES)  # ~170 words: used with a longer duration in tests that need it

SHORT_SENTENCES = SCRIPT_SENTENCES[:9]  # ~105 words: fits the default 52 s Short


def concept_json():
    return json.dumps({"premise": "A student finds a locked hostel room that is waiting for her.",
                       "central_mystery": "Who lives in room thirteen?", "main_character": "Mira, 21, student",
                       "ending_direction": "twist: the room was prepared for her", "setting": "old hostel, midnight",
                       "research_topic": ""})


def bible_json():
    return json.dumps({
        "title": "Room Thirteen", "palette": "sodium yellow and cold blue", "time_of_day": "midnight", "weather": "fog",
        "characters": [{"id": "mira", "name": "Mira", "age": "21", "appearance": "short black hair, tired eyes",
                        "clothing": "black hoodie, blue jeans", "features": "small scar on chin", "role": "protagonist"}],
        "locations": [{"id": "hostel_corridor", "name": "hostel corridor", "description": "long narrow corridor, peeling green paint",
                       "layout": "doors on the left, window at the end", "lighting": "flickering tube light",
                       "palette": "green and yellow", "weather": "fog outside", "time_of_day": "midnight"}],
        "props": [{"id": "note", "name": "note", "description": "folded paper"}],
        "entities": [], "continuity_rules": ["the corridor light always flickers"]})


def script_json(sentences=SHORT_SENTENCES):
    return json.dumps({"script": " ".join(sentences), "episode_summary": "Mira finds room thirteen waiting for her."})


def scenes_json(n_sentences=len(SHORT_SENTENCES), per=1):
    items, i = [], 1
    while i <= n_sentences:
        j = min(i + per, n_sentences)
        items.append({"first_sentence": i, "last_sentence": j, "location": "hostel_corridor", "characters": ["mira"],
                      "action": "Mira walks the corridor", "camera": "slow push-in", "lighting": "flickering",
                      "atmosphere": "dread", "keywords": ["empty hostel corridor", "flickering light corridor", "dark hallway"],
                      "sfx": ["door creak"] if i in (3, 7) else [], "ambience": "fog outside", "transition": "cut",
                      "pause_after": 0.3})
        i = j + 1
    return json.dumps(items)


def seo_json():
    return json.dumps({"title": "Room Thirteen Was Waiting", "description": "A student finds a door that should be locked.",
                       "tags": ["hostel horror", "locked room"], "thumbnail_text": "ROOM 13",
                       "thumbnail_concept": "Warm light under a dark door"})


class FakeLLM:
    """Routes by prompt content, records calls. `overrides` maps a marker to a replacement answer."""
    def __init__(self, overrides=None, fail=False):
        self.calls, self.overrides, self.fail = [], overrides or {}, fail

    def __call__(self, prompt):
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("quota exceeded")
        for marker, answer in self.overrides.items():
            if marker in prompt:
                return types.SimpleNamespace(content=answer)
        if "horror story editor" in prompt:
            ans = concept_json()
        elif "STORY BIBLE" in prompt:
            ans = bible_json()
        elif "narration script" in prompt:
            ans = script_json()
        elif "planning" in prompt and "scenes" in prompt:
            ans = scenes_json()
        elif "YouTube metadata" in prompt:
            ans = seo_json()
        else:
            ans = "{}"
        return types.SimpleNamespace(content=ans)


def make_test_clip(path, seconds=2, size="160x284"):
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=15",
                    "-pix_fmt", "yuv420p", path], check=True, capture_output=True)
    return path


class FakePexels:
    """search/download mocks. `unrelated=True` returns footage whose title does not match the query."""
    def __init__(self, clip_path, unrelated=False, empty=False):
        self.clip, self.unrelated, self.empty, self.n, self.searched = clip_path, unrelated, empty, 0, []

    def search(self, query, orientation="portrait"):
        self.searched.append(query)
        if self.empty:
            return []
        self.n += 1
        slug = "pizza-cooking-in-oven" if self.unrelated else query.replace(" ", "-")
        return [{"id": 1000 + self.n, "width": 1080, "height": 1920, "duration": 8, "query": query,
                 "url": f"https://www.pexels.com/video/{slug}-{1000 + self.n}/", "files": []}]

    def download(self, cand, path):
        shutil.copy(self.clip, path)


def fake_synth(text, path, voice, rate, pitch):
    from pydub import AudioSegment
    AudioSegment.silent(duration=int(max(len(text.split()) / 2.3, 1.0) * 1000)).export(path, format="mp3").close()


class FakeMeta:
    def __init__(self):
        self.d = {}

    def get_meta(self, k):
        return self.d.get(k)

    def set_meta(self, k, v):
        self.d[k] = v


def make_cfg(**kw):
    base = dict(category="supernatural", upload=False, music=False, subtitles=False)
    base.update(kw)
    cfg = HorrorConfig(**base)
    cfg.validate()
    return cfg
