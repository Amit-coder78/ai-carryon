"""Offline tests for the Hindi/Cricket voice settings (no network, no Sarvam, no Edge).

    python -m unittest tests.test_voice_config -v
"""
import importlib
import os
import sys
import types
import unittest

VAR_NAMES = ["SARVAM_API_KEY"] + [
    f"{c}_{v}" for c in ("HINDI", "CRICKET")
    for v in ("SARVAM_SPEAKER", "SARVAM_FALLBACK_SPEAKER", "EDGE_VOICE", "EDGE_RATE", "EDGE_PITCH")
]


def _write(path, data, mode="wb"):
    with open(path, mode) as f:
        f.write(data)


class _FakeCommunicate:
    last = None

    def __init__(self, text, voice, **kw):
        _FakeCommunicate.last = {"text": text, "voice": voice, **kw}

    async def save(self, path):
        _write(path, b"fake", "wb")


class VoiceConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in VAR_NAMES}
        for k in VAR_NAMES:
            os.environ.pop(k, None)
        self._edge = sys.modules.get("edge_tts")
        sys.modules["edge_tts"] = types.SimpleNamespace(Communicate=_FakeCommunicate)
        _FakeCommunicate.last = None
        self._cwd = os.getcwd()
        import tempfile
        self._tmp = tempfile.mkdtemp()
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._edge is None:
            sys.modules.pop("edge_tts", None)
        else:
            sys.modules["edge_tts"] = self._edge

    def _load(self, modname):
        mod = importlib.import_module(modname)
        return importlib.reload(mod)

    def test_defaults_unchanged(self):
        for modname in ("agents_hindi.voice_agent", "agents_cricket.voice_agent"):
            m = self._load(modname)
            self.assertEqual((m.PRIMARY_SPEAKER, m.FALLBACK_SPEAKER), ("shubh", "vijay"))
            m.generate_voice("नमस्ते")  # no SARVAM_API_KEY -> edge fallback
            self.assertEqual(_FakeCommunicate.last["voice"], "hi-IN-MadhurNeural")
            self.assertEqual(_FakeCommunicate.last["rate"], "+0%")
            self.assertEqual(_FakeCommunicate.last["pitch"], "+0Hz")

    def test_empty_env_values_fall_back_to_defaults(self):
        # GitHub passes unset repo variables as empty strings
        for k in VAR_NAMES[1:]:
            os.environ[k] = ""
        m = self._load("agents_hindi.voice_agent")
        self.assertEqual(m.PRIMARY_SPEAKER, "shubh")
        m.generate_voice("x")
        self.assertEqual(_FakeCommunicate.last["voice"], "hi-IN-MadhurNeural")

    def test_channels_are_configured_independently(self):
        os.environ.update(HINDI_EDGE_VOICE="hi-IN-SwaraNeural", HINDI_EDGE_RATE="+10%", HINDI_EDGE_PITCH="+3Hz",
                          CRICKET_EDGE_VOICE="hi-IN-MadhurNeural", CRICKET_EDGE_RATE="+12%", CRICKET_EDGE_PITCH="+2Hz",
                          HINDI_SARVAM_SPEAKER="ritu", CRICKET_SARVAM_SPEAKER="shubh")
        h = self._load("agents_hindi.voice_agent")
        h.generate_voice("x")
        self.assertEqual(_FakeCommunicate.last["voice"], "hi-IN-SwaraNeural")
        self.assertEqual(_FakeCommunicate.last["rate"], "+10%")
        self.assertEqual(h.PRIMARY_SPEAKER, "ritu")
        c = self._load("agents_cricket.voice_agent")
        c.generate_voice("x")
        self.assertEqual(_FakeCommunicate.last["voice"], "hi-IN-MadhurNeural")
        self.assertEqual(_FakeCommunicate.last["pitch"], "+2Hz")
        self.assertEqual(c.PRIMARY_SPEAKER, "shubh")


if __name__ == "__main__":
    unittest.main()
