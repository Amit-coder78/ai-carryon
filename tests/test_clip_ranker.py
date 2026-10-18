"""Offline tests for the shared clip ranker (no network, no LLM, no Pexels).

    python -m pytest tests/test_clip_ranker.py -q
"""
import pytest

from agents import clip_ranker as cr


def cand(cid, slug, w=1080, h=1920, dur=8):
    return {"id": cid, "url": f"https://www.pexels.com/video/{slug}-{cid}/", "width": w,
            "height": h, "duration": dur,
            "files": [{"file_type": "video/mp4", "width": 1080, "link": f"https://cdn/{cid}.mp4"}]}


def ids(ranked):
    return [c["id"] for c in ranked]


def test_screwdriver_beats_car_driver():
    pool = [cand(1, "car-driver-at-night"), cand(2, "man-using-a-screwdriver")]
    assert ids(cr.rank_for_query("screwdriver", pool)) == [2]


def test_compound_word_matches_both_ways():
    assert cr.score_title("screwdriver", "man using screw driver")[0] == 1.0
    assert cr.score_title("wi fi router", "white wifi router on table")[0] == 1.0


def test_plural_and_ing_forms():
    assert cr.score_title("wifi routers", "wifi router lights")[0] == 1.0
    assert cr.score_title("tightening screw", "hand tightens screw")[0] == 1.0


def test_reject_only_when_subject_missing_from_title():
    rel, rejected = cr.score_title("screwdriver hand", "hand holding car key")
    assert rejected
    rel, rejected = cr.score_title("screwdriver hand", "screwdriver repairing car engine")
    assert not rejected and rel == 0.5


def test_ambiguous_word_is_rejected_even_when_present():
    assert cr.score_title("python code", "python snake on a branch")[1]
    assert not cr.score_title("python code", "python code on laptop screen")[1]


def test_cricket_insect_is_rejected_but_cricket_bat_is_kept():
    assert cr.score_title("cricket bat", "cricket insect on leaf")[1]
    assert not cr.score_title("cricket bat", "cricket bat and ball on grass")[1]


def test_channel_reject_words():
    rel, rej = cr.score_title("stadium crowd", "football stadium crowd", cr.CHANNEL_REJECT["cricket"])
    assert rej
    assert not cr.score_title("stadium crowd", "football stadium crowd")[1]


def test_nothing_relevant_returns_empty_so_caller_can_retry():
    pool = [cand(1, "friends-laughing-at-party"), cand(2, "sunset-over-sea")]
    assert cr.rank_for_query("screwdriver", pool) == []


def test_better_match_wins_and_style_words_do_not_count():
    pool = [cand(1, "close-up-of-hand"), cand(2, "hand-holding-screwdriver")]
    assert ids(cr.rank_for_query("screwdriver hand close up", pool))[0] == 2
    assert cr.score_title("close up", "close up of flower")[0] == 0.0  # no subject words


def test_exclude_ids_and_duration_window():
    pool = [cand(1, "wifi-router", dur=8), cand(2, "wifi-router", dur=40), cand(3, "wifi-router", dur=8)]
    assert ids(cr.rank_for_query("wifi router", pool, 3, 25, exclude_ids={3})) == [1]


def test_portrait_hd_preferred_among_equal_titles():
    pool = [cand(1, "wifi-router", w=1920, h=1080), cand(2, "wifi-router")]
    assert ids(cr.rank_for_query("wifi router", pool))[0] == 2


def test_compact_and_broaden_query_keep_the_noun():
    assert cr.compact_query("a close up of the screwdriver tightening a screw today", 5) == \
        "close up screwdriver tightening screw"
    assert cr.broaden_query("close up of screwdriver tightening screw") == "screwdriver tightening"
    assert cr.broaden_query("close up") == ""


def test_flags(monkeypatch):
    assert cr.enabled("english")
    monkeypatch.setenv("SEMANTIC_CLIPS_HINDI", "0")
    assert not cr.enabled("hindi") and cr.enabled("english")
    monkeypatch.setenv("SEMANTIC_CLIPS", "0")
    assert not cr.enabled("english")


# ───────── integration: English / Bhakti search path ─────────
class _Resp:
    def __init__(self, videos):
        self._v = videos

    def raise_for_status(self):
        pass

    def json(self):
        return {"videos": self._v}


def _pexels_video(c):
    return {"id": c["id"], "url": c["url"], "width": c["width"], "height": c["height"],
            "duration": c["duration"], "video_files": c["files"]}


@pytest.fixture
def vca(monkeypatch):
    mod = pytest.importorskip("agents.video_clip_agent")
    monkeypatch.setenv("PEXELS_API_KEY", "test")
    return mod


def test_english_search_picks_matching_clip_not_first(vca, monkeypatch):
    pool = [cand(1, "car-driver-at-night"), cand(2, "man-using-a-screwdriver")]
    calls = []
    monkeypatch.setattr(vca.requests, "get",
                        lambda *a, **k: calls.append(k["params"]) or _Resp([_pexels_video(c) for c in pool]))
    used = set()
    assert vca.search_pexels_video("screwdriver", channel="english", used_ids=used) == "https://cdn/2.mp4"
    assert used == {2} and len(calls) == 1 and calls[0]["per_page"] == 15


def test_english_search_flag_off_is_old_behaviour(vca, monkeypatch):
    monkeypatch.setenv("SEMANTIC_CLIPS", "0")
    pool = [cand(1, "car-driver-at-night")]
    calls = []
    monkeypatch.setattr(vca.requests, "get",
                        lambda *a, **k: calls.append(k["params"]) or _Resp([_pexels_video(c) for c in pool]))
    assert vca.search_pexels_video("screwdriver") == "https://cdn/1.mp4"
    assert calls[0]["per_page"] == 1


def test_english_search_returns_none_when_nothing_relevant(vca, monkeypatch):
    pool = [cand(1, "car-driver-at-night")]
    monkeypatch.setattr(vca.requests, "get", lambda *a, **k: _Resp([_pexels_video(c) for c in pool]))
    assert vca.search_pexels_video("screwdriver") is None


def test_english_search_falls_back_if_ranker_crashes(vca, monkeypatch):
    pool = [cand(1, "car-driver-at-night")]
    monkeypatch.setattr(vca.requests, "get", lambda *a, **k: _Resp([_pexels_video(c) for c in pool]))
    monkeypatch.setattr(cr, "rank_for_query", lambda *a, **k: 1 / 0)
    assert vca.search_pexels_video("screwdriver") == "https://cdn/1.mp4"


# ───────── integration: Hindi / Cricket ranking ─────────
def _hcand(cid, slug, query="screwdriver"):
    c = cand(cid, slug)
    c["query"] = query
    return c


def test_hindi_ranking_pushes_wrong_meaning_down():
    hi = pytest.importorskip("agents_hindi.video_clip_agent")
    pool = [_hcand(1, "car-driver-at-night"), _hcand(2, "friends-laughing"), _hcand(3, "screw-driver-closeup")]
    assert [c["id"] for c in hi.rank_candidates([pool], set(), 8)] == [3, 2, 1]


def test_hindi_ranking_flag_off_restores_old_scores(monkeypatch):
    hi = pytest.importorskip("agents_hindi.video_clip_agent")
    monkeypatch.setenv("SEMANTIC_CLIPS_HINDI", "0")
    pool = [_hcand(1, "car-driver-at-night"), _hcand(3, "screw-driver-closeup")]
    # old scorer only matches whole slug words, so "screw-driver" gets no credit
    assert hi.score_candidate(pool[1], 0, 8) == hi.score_candidate(pool[0], 0, 8)
