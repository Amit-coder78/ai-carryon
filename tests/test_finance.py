"""Offline tests for AI CarryON Finance. No network, no paid APIs. Run: python -m unittest tests.test_finance -v"""
import json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

from agents_finance import calculations as calc
from agents_finance import fact_checker as fc
from agents_finance import llm, market_data, research, scene_planner, topics
from agents_finance.config import ConfigError, FinanceSettings
from agents_finance.database import MemoryStore
from agents_finance.facts import Fact, render_template, ungrounded_numbers
from agents_finance.metadata import build_metadata
from agents_finance.script_agent import ScriptRejected, ScriptResult, generate_script, parse_json
from agents_finance.sources import SourceRecord, classify_tier

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def S(**kw):
    return FinanceSettings(**kw)


def close_fact(age_h=10, fid="t_close"):
    return Fact(fid, "Nifty latest close", 24000.5, "current", "count", as_of=(NOW - timedelta(hours=age_h)).isoformat(),
                source_url="https://stooq.com/q/?s=^nsei", source_title="stooq")


class TestCalculations(unittest.TestCase):
    def test_values(self):
        self.assertAlmostEqual(calc.pct_change(100, 110), 10.0)
        self.assertAlmostEqual(calc.cagr(100, 200, 5), 14.8698, places=3)
        self.assertAlmostEqual(calc.dividend_yield(5, 200), 2.5)
        self.assertAlmostEqual(calc.pe_ratio(100, 5), 20.0)
        self.assertAlmostEqual(calc.risk_reward(100, 95, 110), 2.0)
        self.assertEqual(calc.position_size(100000, 1, 100, 95), 200)
        self.assertAlmostEqual(calc.convert_currency(10, 83.5), 835.0)
        self.assertAlmostEqual(calc.lumpsum_future_value(1000, 10, 2), 1210.0)

    def test_sip_matches_closed_form(self):
        i = 0.12 / 12
        expect = 5000 * (((1 + i) ** 120 - 1) / i) * (1 + i)
        self.assertAlmostEqual(calc.sip_future_value(5000, 12, 10), expect, places=4)
        self.assertEqual(calc.sip_future_value(1000, 0, 1), 12000)

    def test_invalid_inputs(self):
        for fn, args in ((calc.pct_change, (0, 5)), (calc.pe_ratio, (100, -2)), (calc.cagr, (-1, 5, 3)),
                         (calc.risk_reward, (100, 105, 110)), (calc.pct_change, (float("nan"), 1)),
                         (calc.pct_change, (True, 1)), (calc.ev_ebitda, (10, 0))):
            with self.assertRaises(calc.CalcError): fn(*args)

    def test_rsi_bounds(self):
        up = list(range(1, 30)); self.assertEqual(calc.rsi(up), 100.0)
        mixed = [10, 11, 10.5, 11.5, 11, 12, 11.2, 12.4, 12, 13, 12.1, 13.3, 12.8, 13.9, 13.2, 14]
        self.assertTrue(0 < calc.rsi(mixed) < 100)


class TestFactsAndGrounding(unittest.TestCase):
    def test_render_and_direction(self):
        f = Fact("c", "chg", -0.8412, "computed", "pct", derived_from=["x"])
        m = {"c": f}
        self.assertEqual(render_template("Index is {{c.dir}} {{c.abs}}.", m), "Index is down 0.84%.")
        self.assertEqual(render_template("{{c}}", m), "-0.84%")

    def test_inr_indian_grouping(self):
        f = Fact("m", "x", 1234567.5, "hypothetical", "money", "INR", 2, note="n")
        self.assertEqual(f.render(), "₹12,34,567.50")

    def test_ungrounded_numbers(self):
        self.assertEqual(ungrounded_numbers("Nifty rose 1.2% and {{a}}"), ["1.2%"])
        self.assertEqual(ungrounded_numbers("Here are 3 things about {{a}}."), [])
        self.assertEqual(ungrounded_numbers("Since 1992", allowed_literals=[1992]), [])
        self.assertEqual(ungrounded_numbers("Since 1992"), ["1992"])


class TestFactChecker(unittest.TestCase):
    def check(self, template, facts, **kw):
        st = kw.pop("settings", S(category="technical_analysis", duration_sec=45, words_per_second=0.5))
        return fc.check_script(template, facts, st, now=NOW, **kw)

    def test_fresh_data_passes(self):
        r = self.check("The index closed at {{t_close}}. Trading carries risk of loss.", [close_fact()], requires_current_data=True)
        self.assertTrue(r.passed, r.summary())

    def test_stale_data_blocks(self):
        r = self.check("Close was {{t_close}}. Risk of loss exists.", [close_fact(age_h=200)])
        self.assertIn("STALE_DATA", [f.code for f in r.errors])

    def test_unknown_placeholder_and_ungrounded(self):
        self.assertIn("UNKNOWN_FACT", [f.code for f in self.check("{{nope}} risk", []).errors])
        self.assertIn("UNGROUNDED_NUMBER", [f.code for f in self.check("It jumped 5.5% today. risk", []).errors])

    def test_banned_language_but_negations_allowed(self):
        bad = self.check("This is a sure shot, guaranteed profit with risk.", [])
        self.assertIn("BANNED_PHRASE", [f.code for f in bad.errors])
        ok = self.check("Past performance does not guarantee results. There is no guarantee and risk of loss.", [])
        self.assertTrue(ok.passed, ok.summary())

    def test_missing_risk_for_derivatives(self):
        st = S(category="derivatives", duration_sec=45, words_per_second=0.5)
        self.assertIn("MISSING_RISK", [f.code for f in fc.check_script("Options give the right to buy.", [], st, now=NOW).errors])

    def test_hypothetical_rules(self):
        f = Fact("h", "Example amount", 5000, "hypothetical", "money", "INR", 0, note="assumes X")
        r = self.check("You put in {{h}} each month. Risk of loss exists.", [f])
        self.assertIn("HYPOTHETICAL_NOT_SIGNPOSTED", [x.code for x in r.errors])
        r2 = self.check("For example, you put in {{h}} each month. Risk of loss exists.", [f])
        self.assertTrue(r2.passed, r2.summary())
        f.note = ""
        self.assertIn("UNLABELED_HYPOTHETICAL", [x.code for x in self.check("For example {{h}} risk", [f]).errors])

    def test_forecast_needs_attribution(self):
        f = Fact("g", "GDP growth forecast", 6.5, "forecast", "pct")
        self.assertIn("UNATTRIBUTED_FORECAST", [x.code for x in self.check("Growth is {{g}} risk", [f]).errors])

    def test_contradiction_and_derivation(self):
        a, b = close_fact(), close_fact(fid="t2"); b.value = 1.0
        self.assertIn("CONTRADICTION", [x.code for x in self.check("{{t_close}} risk", [a, b]).errors])
        c = Fact("c", "chg", 1.0, "computed", "pct", derived_from=["missing"])
        self.assertIn("BAD_DERIVATION", [x.code for x in self.check("{{c}} risk", [c]).errors])

    def test_current_data_required(self):
        r = self.check("Markets are interesting. Risk of loss.", [], requires_current_data=True)
        self.assertIn("NO_VERIFIED_CURRENT_DATA", [x.code for x in r.errors])


class TestSources(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(classify_tier("https://www.nseindia.com/x"), 1)
        self.assertEqual(classify_tier("https://www.rbi.org.in/y"), 3)
        self.assertEqual(classify_tier("https://economictimes.indiatimes.com/z"), 5)
        self.assertEqual(classify_tier("https://random-blog.example/a"), 9)
        self.assertEqual(SourceRecord(url="https://www.sebi.gov.in/a", title="t").tier, 3)


CSV = "Date,Open,High,Low,Close,Volume\n2026-10-20,100,105,99,102,10\n2026-10-21,102,108,101,106,12\n2026-10-22,106,107,100,104,9\n"


class TestMarketData(unittest.TestCase):
    def test_parse_and_facts(self):
        p = market_data.StooqProvider(fetch=lambda u: CSV)
        bars = p.daily_bars("^NSEI", days=30)
        self.assertEqual([b.close for b in bars], [102, 106, 104])
        close, chg, series = market_data.build_close_facts("t", "Nifty", "^NSEI", bars, "", p)
        self.assertAlmostEqual(chg.value, calc.pct_change(106, 104))
        self.assertEqual(len(series.series), 3)
        self.assertEqual(close.kind, "current"); self.assertTrue(close.source_url)

    def test_failures(self):
        def boom(u): raise OSError("network down")
        with self.assertRaises(market_data.MarketDataError): market_data.StooqProvider(fetch=boom).daily_bars("X")
        for body in ("No data", "", "Date,Open\n"):
            with self.assertRaises(market_data.MarketDataError): market_data.StooqProvider(fetch=lambda u, b=body: b).daily_bars("X")


RSS = """<rss><channel><item><title>RBI keeps repo rate unchanged</title><link>https://www.rbi.org.in/a</link>
<pubDate>Thu, 22 Oct 2026 08:00:00 +0000</pubDate></item>
<item><title>Old item</title><link>https://www.rbi.org.in/b</link><pubDate>Mon, 01 Jun 2026 08:00:00 +0000</pubDate></item>
<item><title>Blog</title><link>https://blog.example/c</link><pubDate>Thu, 22 Oct 2026 09:00:00 +0000</pubDate></item></channel></rss>"""


class TestResearchAndTopics(unittest.TestCase):
    def test_gather_filters_stale_and_unrated(self):
        items, errs = research.gather(S(content_type="news", category="news"), [{"url": "u", "publisher": "RBI"}], fetch=lambda u: RSS, now=NOW)
        self.assertEqual([i["title"] for i in items], ["RBI keeps repo rate unchanged"])
        self.assertEqual(items[0]["source"]["tier"], 3)

    def test_feed_error_is_reported_not_raised(self):
        def boom(u): raise OSError("dns")
        items, errs = research.gather(S(), [{"url": "u"}], fetch=boom, now=NOW)
        self.assertEqual(items, []); self.assertEqual(len(errs), 1)

    def test_news_without_research_skips(self):
        self.assertIsNone(topics.select_topic(S(content_type="news", category="news"), [], set(), NOW))

    def test_news_dedup_and_selection(self):
        items, _ = research.gather(S(content_type="news", category="news"), [{"url": "u"}], fetch=lambda u: RSS, now=NOW)
        t = topics.select_topic(S(content_type="news", category="news"), items, set(), NOW)
        self.assertTrue(t.id.startswith("news:"))
        self.assertIsNone(topics.select_topic(S(content_type="news", category="news"), items, {t.id}, NOW))

    def test_evergreen_never_repeats_and_exhausts(self):
        st, seen = S(category="investing"), set()
        for _ in range(3):
            t = topics.select_topic(st, [], seen, NOW); self.assertIsNotNone(t); seen.add(t.id)
        self.assertIsNone(topics.select_topic(st, [], seen, NOW))

    def test_analytical_needs_ticker(self):
        self.assertIsNone(topics.select_topic(S(content_type="analytical"), [], set(), NOW))

    def test_example_facts_deterministic_and_labeled(self):
        t = topics.select_topic(S(category="investing"), [], set(), NOW)
        fs = topics.example_facts(t)
        self.assertTrue(all(f.kind in ("hypothetical", "computed") and f.note for f in fs))
        self.assertAlmostEqual(next(f for f in fs if f.id == "ex_fv").value, calc.sip_future_value(5000, 12, 10))


GOOD = ('For example, imagine investing {{ex_monthly}} every month for {{ex_years}} years at an assumed {{ex_rate}} yearly return. '
        'The projected value in this example would be {{ex_fv}}. Real returns vary, and you can lose money, so there is risk of loss. '
        'Compounding rewards patience, but it is no guarantee of results.')


def fake_invoke_factory(outputs):
    it = iter(outputs)
    def inv(prompt, system=""):
        return next(it)
    return inv


class TestScriptAndScenes(unittest.TestCase):
    def setUp(self):
        self.settings = S(category="investing", duration_sec=45, words_per_second=0.9)
        self.topic = topics.select_topic(self.settings, [], set(), NOW)
        self.facts = topics.example_facts(self.topic)

    def test_script_ok(self):
        out = json.dumps({"narration": GOOD, "visual_hints": ["savings jar", "ACME Corp logo"], "title_ideas": ["How compounding works"]})
        r = generate_script(self.topic, self.facts, self.settings, fake_invoke_factory([out]), now=NOW)
        self.assertIn("₹", r.narration); self.assertNotIn("{{", r.narration)

    def test_retry_then_success_and_feedback_given(self):
        bad = json.dumps({"narration": "You will earn 15% guaranteed returns."})
        good = json.dumps({"narration": GOOD})
        seen = []
        outs = iter([bad, "not json at all", good])
        def inv(prompt, system=""): seen.append(prompt); return next(outs)
        r = generate_script(self.topic, self.facts, self.settings, inv, now=NOW)
        self.assertTrue(r.narration)
        self.assertIn("UNGROUNDED_NUMBER", seen[1])      # feedback reached the retry prompt

    def test_rejected_after_retries(self):
        bad = json.dumps({"narration": "Guaranteed 20% returns, buy now."})
        with self.assertRaises(ScriptRejected):
            generate_script(self.topic, self.facts, self.settings, fake_invoke_factory([bad] * 3), now=NOW)

    def test_scene_planning(self):
        sc = scene_planner.plan_scenes(GOOD, self.facts, self.topic, self.settings, ["savings jar", "ACME Corp logo", "price 500"])
        self.assertAlmostEqual(sum(s.duration_sec for s in sc), 45, delta=1.0)
        self.assertTrue(all(s.visual_type == "stock" and s.search_terms for s in sc))   # no series => no fake chart
        self.assertTrue(any(s.source_attribution for s in sc))
        self.assertIn("Hypothetical", " ".join(a for s in sc for a in s.source_attribution))

    def test_chart_scene_only_for_verified_series_and_generic_terms(self):
        p = market_data.StooqProvider(fetch=lambda u: CSV)
        bars = p.daily_bars("RELIANCE.NS")
        facts = market_data.build_close_facts("t", "RELIANCE", "RELIANCE.NS", bars, "INR", p)
        st = S(category="company_analysis", content_type="analytical", ticker="RELIANCE", duration_sec=45, words_per_second=0.6)
        topic = topics.FinanceTopic("a", "company_analysis", "analytical", "india", "RELIANCE: what the numbers show", "RELIANCE", entity_terms=["RELIANCE"])
        tpl = "The stock is {{t_chg.dir}} {{t_chg.abs}} versus the previous close. Here is the recent trend {{t_series}}. Investing carries risk of loss. Facts are not advice."
        sc = scene_planner.plan_scenes(tpl, facts, topic, st, ["RELIANCE refinery", "oil tanker"])
        charts = [s for s in sc if s.visual_type == "chart"]
        self.assertEqual(len(charts), 1); self.assertEqual(len(charts[0].chart["series"]), 3)
        for s in sc:
            for t in s.search_terms: self.assertNotIn("reliance", t.lower())

    def test_validate_rejects_bad_scenes(self):
        st = S(duration_sec=45)
        with self.assertRaises(scene_planner.SceneError):
            scene_planner.validate_scenes([scene_planner.Scene(0, "x", 45, "chart")], st)
        with self.assertRaises(scene_planner.SceneError):
            scene_planner.validate_scenes([scene_planner.Scene(0, "x", 45, "stock", ["acme logo"])], st, forbidden=["acme"])

    def test_metadata_safe(self):
        res = ScriptResult(template=GOOD, narration=render_template(GOOD, {f.id: f for f in self.facts}),
                           title_ideas=["Guaranteed profits with SIP", "How compounding works"])
        m = build_metadata(self.topic, res, self.facts, self.settings)
        self.assertEqual(m["title"], "How compounding works")
        self.assertIn("not investment advice", m["description"]); self.assertIn("hypothetical", m["description"])


class TestConfigAndIsolation(unittest.TestCase):
    def test_defaults_and_env(self):
        s = FinanceSettings.from_env({})
        self.assertEqual((s.language, s.video_format, s.duration_sec, s.require_review), ("en", "shorts", 55, True))
        s = FinanceSettings.from_env({"FINANCE_FORMAT": "long", "FINANCE_CATEGORY": "derivatives", "FINANCE_TICKER": "reliance"})
        self.assertEqual((s.duration_sec, s.ticker), (300, "RELIANCE"))

    def test_invalid_config(self):
        for env in ({"FINANCE_DURATION_SEC": "90"}, {"FINANCE_CATEGORY": "crypto"}, {"FINANCE_LANGUAGE": "hi"},
                    {"FINANCE_TICKER": "bad ticker!"}, {"FINANCE_MAX_RETRIES": "x"}):
            with self.assertRaises(ConfigError): FinanceSettings.from_env(env)

    def test_ignores_other_channel_env(self):
        env = {"BHAKTI_MODE": "music_only", "COMMENT_AUTO_REPLY": "false", "HOOK_ENGINE": "0"}
        self.assertEqual(FinanceSettings.from_env(env), FinanceSettings.from_env({}))

    def test_isolation(self):
        code = ("import sys, agents_finance.config, agents_finance.fact_checker, agents_finance.scene_planner, agents_finance.topics, "
                "agents_finance.script_agent, agents_finance.media, agents_finance.metadata;"
                "bad=[m for m in sys.modules if m.split('.')[0] in ('agents','agents_cricket','agents_hindi','agents_bhakti','agents_gaming')];"
                "sys.exit(1 if bad else 0)")
        self.assertEqual(subprocess.run([sys.executable, "-c", code], cwd=os.getcwd()).returncode, 0)


class TestLLMAndMedia(unittest.TestCase):
    def test_groq_then_gemini_fallback(self):
        calls = []
        def post(url, payload, headers):
            calls.append(url)
            if "groq" in url: raise OSError("429")
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        self.assertEqual(llm.invoke("p", post=post, env={"GROQ_API_KEY": "a", "GEMINI_API_KEY": "b"}), "ok")
        self.assertEqual(len(calls), 2)
        with self.assertRaises(llm.LLMError): llm.invoke("p", post=post, env={})

    def test_media_chart_and_stock_and_fallback(self):
        from agents_finance.media import fetch_scene_media
        stock = scene_planner.Scene(0, "a", 5, "stock", ["financial charts monitor"])
        chart = scene_planner.Scene(1, "b", 5, "chart", chart={"series": [["2026-10-20", 1], ["2026-10-21", 2]], "title": "t", "as_of": "2026-10-21", "source": "s"})
        made = []
        def clip(q, out): open(out, "wb").write(b"x"); made.append(q)
        def bad_chart(c, out): raise RuntimeError("no mpl")
        with tempfile.TemporaryDirectory() as d:
            paths, errs = fetch_scene_media([stock, chart], out_dir=os.path.join(d, "c"), clip_fn=clip, chart_fn=bad_chart)
            self.assertEqual(len(paths), 2); self.assertEqual(len(errs), 1)
            self.assertEqual(made[1], "stock market trading screen")   # generic fallback only

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "ffmpeg missing")
    def test_real_chart_render_to_mp4(self):
        try: import matplotlib  # noqa
        except ImportError: self.skipTest("matplotlib missing")
        from agents_finance import charts
        with tempfile.TemporaryDirectory() as d:
            png = charts.render_chart_png({"series": [[f"2026-10-{i:02d}", 100 + i] for i in range(1, 11)], "title": "Demo", "as_of": "2026-10-10T23:59", "source": "stooq"}, os.path.join(d, "c.png"))
            mp4 = charts.png_to_clip(png, os.path.join(d, "c.mp4"), 2)
            self.assertGreater(os.path.getsize(mp4), 1000)


class TestSchedulerEndToEnd(unittest.TestCase):
    """Full pipeline with every external service faked; also checks resume + review gate + no-data skip."""
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.cwd = os.getcwd(); os.chdir(self.d)
        import types
        rs = types.ModuleType("agents.run_state")
        class RunState:
            _db = {}
            def __init__(s, get, setm): s.get, s.setm = get, setm; s.ctx = json.loads(get("rs") or "null") or {}; s.resumed = bool(s.ctx)
            @classmethod
            def open(cls, ch, get, setm): return cls(get, setm)
            def _save(s): s.setm("rs", json.dumps(s.ctx))
            def begin(s, c): s.ctx = dict(c); s._save()
            def step(s, name, fn, validate=None, after=None):
                if name in s.ctx.get("_done", {}): return s.ctx["_done"][name]
                v = fn()
                if validate and not validate(v): raise RuntimeError(f"step {name} invalid")
                s.ctx.setdefault("_done", {})[name] = v; s._save(); return v
            def has(s, n): return n in s.ctx.get("_done", {})
            def forget(s, n): s.ctx.get("_done", {}).pop(n, None)
            def clear(s): s.ctx = {}; s.setm("rs", "")
        rs.RunState = RunState
        pkg = types.ModuleType("agents"); pkg.run_state = rs
        sys.modules["agents"], sys.modules["agents.run_state"] = pkg, rs
        sys.path.insert(0, self.cwd)

    def tearDown(self):
        os.chdir(self.cwd)
        for m in ("agents", "agents.run_state"): sys.modules.pop(m, None)

    def deps(self, outputs, store=None, provider=None):
        import scheduler_finance as sf
        def voice(text, output_path): open(output_path, "wb").write(b"a")
        def caps(text, audio_path): open("output/captions.srt", "w").write("1")
        def video(use_pexels_clips, hook): open("output/final_video.mp4", "wb").write(b"v")
        def clip(q, out): open(out, "wb").write(b"c")
        return sf.Deps(store=store or MemoryStore(), invoke=fake_invoke_factory(outputs), voice=voice, captions=caps,
                       video=video, clip_fn=clip, provider=provider or market_data.StooqProvider(fetch=lambda u: CSV))

    def test_educational_to_review_bundle(self):
        import scheduler_finance as sf
        s = S(category="investing", duration_sec=45, words_per_second=0.9)
        deps = self.deps([json.dumps({"narration": GOOD, "title_ideas": ["How compounding works"]})])
        r = sf.run_finance_cycle(s, deps)
        self.assertEqual(r["status"], "ready_for_review", r)
        self.assertTrue(os.path.exists(os.path.join(r["bundle"], "video.mp4")))
        self.assertEqual(len(deps.store.posted_ids()), 1)
        r2 = sf.run_finance_cycle(s, self.deps([json.dumps({"narration": GOOD})], store=deps.store))
        self.assertEqual(r2["status"], "ready_for_review")       # next evergreen topic, not a repeat
        self.assertEqual(len(deps.store.posted_ids()), 2)

    def test_analytical_without_data_is_skipped_not_invented(self):
        import scheduler_finance as sf
        def boom(u): raise OSError("down")
        s = S(category="company_analysis", content_type="analytical", ticker="TCS", duration_sec=45)
        deps = self.deps([], provider=market_data.StooqProvider(fetch=boom))
        self.assertEqual(sf.run_finance_cycle(s, deps)["status"], "data_unavailable")
        self.assertEqual(deps.store.posted_ids(), set())

    def test_news_without_feeds_skips(self):
        import scheduler_finance as sf
        s = S(category="news", content_type="news")
        self.assertEqual(sf.run_finance_cycle(s, self.deps([]))["status"], "no_grounded_topic")

    def test_fact_check_failure_blocks_render(self):
        import scheduler_finance as sf
        s = S(category="investing", duration_sec=45, words_per_second=0.9)
        deps = self.deps([json.dumps({"narration": "Guaranteed 18% returns, buy now."})] * 3)
        self.assertEqual(sf.run_finance_cycle(s, deps)["status"], "fact_check_failed")
        self.assertFalse(os.path.exists("output/final_video.mp4"))


if __name__ == "__main__":
    unittest.main()
