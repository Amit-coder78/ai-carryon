"""Finance creator-style script: python -m unittest tests.test_finance_style -v"""
import unittest

from agents_finance import fact_checker as fc, prompts
from tests.test_finance import NOW, S, close_fact


class FinanceStyle(unittest.TestCase):
    def test_style_block_present_and_rules_still_first(self):
        s = prompts.SYSTEM
        self.assertIn("VOICE AND STYLE", s)
        self.assertLess(s.index("Hard rules"), s.index("VOICE AND STYLE"))
        self.assertIn("hard rules above always win", s)
        self.assertTrue(s.rstrip().endswith('"title_ideas": [str]}'))   # JSON contract unchanged

    def test_creator_style_sample_passes_fact_check(self):
        sample = ("Did the market just trap you? Look, here's the thing. The index closed at {{t_close}}. "
                  "That's the fact. My take, and it's a take, not a fact, is that one day proves nothing. "
                  "And be real with yourself, you can lose money here. So what's your plan, patience or panic?")
        st = S(category="technical_analysis", duration_sec=20, words_per_second=2.0)
        r = fc.check_script(sample, [close_fact()], st, requires_current_data=True, now=NOW)
        self.assertTrue(r.passed, r.summary())

    def test_hype_still_blocked(self):
        st = S(category="technical_analysis", duration_sec=20, words_per_second=2.0)
        r = fc.check_script("Buy now, this is a sure shot. Risk exists.", [], st, now=NOW)
        self.assertIn("BANNED_PHRASE", [f.code for f in r.errors])

    def test_voice_defaults_and_overrides(self):
        import importlib, os
        from agents_finance import voice_en
        for k in ("FINANCE_EDGE_RATE", "FINANCE_EDGE_PITCH"): os.environ.pop(k, None)
        importlib.reload(voice_en)
        self.assertEqual((voice_en.RATE, voice_en.PITCH), ("+8%", "-2Hz"))
        os.environ["FINANCE_EDGE_RATE"] = "+0%"
        try:
            importlib.reload(voice_en); self.assertEqual(voice_en.RATE, "+0%")
        finally:
            os.environ.pop("FINANCE_EDGE_RATE", None); importlib.reload(voice_en)


if __name__ == "__main__":
    unittest.main()
