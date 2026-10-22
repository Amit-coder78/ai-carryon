from agents_hindi.categories import (
    CATEGORIES, pick_category, classify_topic, normalize_category, looks_hindi,
)


def _write(path, data, mode="wb"):
    with open(path, mode) as f:
        f.write(data)


def test_pick_category_avoids_last_one():
    for _ in range(50):
        assert pick_category(last_category="science") != "science"


def test_pick_category_only_allowed():
    for _ in range(50):
        assert pick_category() in CATEGORIES


def test_normalize_category_fixes_bad_llm_value():
    assert normalize_category("history", "iPhone water test") == "gadgets"
    assert normalize_category("", "nothing matches here") == "science"
    assert normalize_category("AI") == "ai"


def test_looks_hindi():
    assert looks_hindi("Yeh liquid aag ki tarah dikhta hai par jalta nahi")
    assert looks_hindi("क्या आपने कभी यह प्रयोग देखा है?")
    assert not looks_hindi("Mix these two liquids and watch the foam rise in seconds")
    assert not looks_hindi("")


def test_spy_filters_by_category(monkeypatch, tmp_path):
    import agents_hindi.spy_agent as spy
    topics = [
        {"category": "ai", "topic": "a", "views": 1},
        {"category": "gadgets", "topic": "b", "views": 1},
    ]
    monkeypatch.setattr(spy, "get_hindi_trending_topics", lambda: topics)
    assert spy.get_best_hindi_topic(category="gadgets")["topic"] == "b"
    assert spy.get_best_hindi_topic(category="tech")["topic"] in ("a", "b")  # falls back


def test_unsupported_script():
    from agents_hindi.categories import has_unsupported_script
    assert has_unsupported_script("భారీ అలల మధ్య పడవ ప్రయాణం Telugu Experiments")
    assert not has_unsupported_script("Dry ice paani mein daalo 😮")
    assert not has_unsupported_script("क्या आपने कभी यह प्रयोग देखा है?")


def test_spy_parses_json_with_extra_text(monkeypatch, tmp_path):
    import types, agents_hindi.spy_agent as spy
    import agents_hindi.model_invoke_agent_hindi as m
    monkeypatch.chdir(tmp_path)
    reply = 'Here you go:\n[{"category": "ai", "topic": "AI vs human drawing", "title": "x"}]\nDone'
    monkeypatch.setattr(m, "safe_invoke", lambda *a, **k: types.SimpleNamespace(content=reply))
    topics = spy.get_hindi_trending_topics()
    assert topics and topics[0]["category"] == "ai"


def test_router_falls_back_to_gemini_on_empty_groq(monkeypatch):
    import types
    import agents_hindi.model_invoke_agent_hindi as m

    class Fake:
        def __init__(self, text): self.text = text
        def invoke(self, prompt): return types.SimpleNamespace(content=self.text)

    m.reset_groq_budget()
    monkeypatch.setattr(m, "_get_groq", lambda *a, **k: Fake(""))
    monkeypatch.setattr(m, "_get_gemini", lambda *a, **k: Fake("from gemini"))
    assert m.safe_invoke("hi").content == "from gemini"


# ───────── Step 2: Pexels clip agent ─────────

def _cand(cid, url, w=1080, h=1920, dur=12, query="wifi router"):
    return {"id": cid, "width": w, "height": h, "duration": dur,
            "url": url, "files": [{"file_type": "video/mp4", "width": 1080, "link": "x"}],
            "query": query}


def test_clean_query():
    from agents_hindi.video_clip_agent import _clean_query
    assert _clean_query("Wi-Fi Router!! close up of the box") == "wi fi router close"


def test_ranking_prefers_matching_title_and_skips_used():
    from agents_hindi.video_clip_agent import rank_candidates
    good = _cand(1, "https://www.pexels.com/video/white-wifi-router-on-table-111/")
    off = _cand(2, "https://www.pexels.com/video/friends-laughing-at-party-222/")
    used = _cand(3, "https://www.pexels.com/video/wifi-router-lights-333/")
    ranked = rank_candidates([[off, good, used]], banned_ids={3}, target_sec=8)
    assert [c["id"] for c in ranked] == [1, 2]


def test_ranking_prefers_clip_long_enough():
    from agents_hindi.video_clip_agent import rank_candidates
    short = _cand(1, "https://www.pexels.com/video/wifi-router-1/", dur=3)
    longer = _cand(2, "https://www.pexels.com/video/wifi-router-2/", dur=10)
    assert rank_candidates([[short, longer]], set(), 8)[0]["id"] == 2


def test_fallback_queries_use_category():
    from agents_hindi.video_clip_agent import _fallback_queries
    scenes = _fallback_queries("Phone water test kaise", "gadgets", 4)
    assert len(scenes) == 4 and scenes[0][0] == "phone water test"
    assert all(len(s) >= 2 for s in scenes)


def test_plan_scene_queries_parses_llm_json(monkeypatch):
    import types
    import agents_hindi.model_invoke_agent_hindi as m
    from agents_hindi.video_clip_agent import plan_scene_queries
    reply = 'ok:\n[{"scene":1,"queries":["Router close-up","wifi lights"]},' \
            '{"scene":2,"queries":["phone signal bars"]}]'
    monkeypatch.setattr(m, "safe_invoke", lambda *a, **k: types.SimpleNamespace(content=reply))
    scenes = plan_scene_queries("wifi", "script words here", 2)
    assert scenes == [["router close up", "wifi lights"], ["phone signal bars"]]


def test_plan_scene_queries_falls_back_when_llm_breaks(monkeypatch):
    import agents_hindi.model_invoke_agent_hindi as m
    from agents_hindi.video_clip_agent import plan_scene_queries

    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(m, "safe_invoke", boom)
    scenes = plan_scene_queries("Dry ice paani mein", "script", 3, "science")
    assert len(scenes) == 3


def test_generate_clips_no_repeats_and_remembers(monkeypatch, tmp_path):
    import agents_hindi.video_clip_agent as v
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(v, "plan_scene_queries",
                        lambda *a, **k: [["a b"], ["a b"], ["a b"]])
    pool = [_cand(i, f"https://www.pexels.com/video/a-b-clip-{i}/", query="a b")
            for i in (1, 2, 3, 4, 5)]
    monkeypatch.setattr(v, "search_candidates", lambda q, **k: list(pool))
    monkeypatch.setattr(v, "_download", lambda c, path: _write(path, "x", "w"))
    saved = {}
    monkeypatch.setattr(v, "_load_used_ids", lambda: [1])          # clip 1 used before
    monkeypatch.setattr(v, "_save_used_ids", lambda ids: saved.setdefault("ids", ids))
    paths, errors = v.generate_background_clips("topic words", "w " * 80, num_clips=3)
    assert len(paths) == 3 and not errors
    assert 1 not in saved["ids"][1:]                 # old id not picked again
    assert len(set(saved["ids"])) == len(saved["ids"]) == 4   # 1 old + 3 new, all distinct
