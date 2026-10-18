#!/usr/bin/env bash
# Semantic clip ranking for the English, Hindi, Cricket and Bhakti pipelines.
# Run from the repo root:  bash apply_semantic_clips.sh
set -e
test -f agents/video_clip_agent.py || { echo "Run this from the ai-carryon repo root"; exit 1; }

cat > agents/clip_ranker.py << 'EOF_RANKER'
# agents/clip_ranker.py
"""
Shared Pexels clip ranking for the English, Hindi, Cricket and Bhakti pipelines.

Zero API calls, zero GPU: it only reads the metadata Pexels already returns
(the clip title hidden in the page URL slug, size, duration).

Why it exists: Pexels search is keyword-based, so a query for "screwdriver" can
return a clip titled "car driver". The old English/Bhakti path asked Pexels for
exactly 1 result and accepted it blindly. Now we ask for a pool of candidates
and keep only clips whose title really matches the query.

Honest limitation: this is lexical matching (stemming, compound words like
"screw driver" == "screwdriver", reject words), not real embeddings. It cannot
know that "wrench" and "spanner" are synonyms. Add embeddings later only if
logs show that matters.

Kill switches:
    SEMANTIC_CLIPS=0                 turns it off everywhere (old behaviour)
    SEMANTIC_CLIPS_<CHANNEL>=0       turns it off for one channel
                                     (ENGLISH, HINDI, CRICKET, BHAKTI)

Contract: callers wrap every use in try/except and fall back to the old
behaviour, so a bug here can never stop a video from rendering.
"""
import os
import re

# Words that carry no subject.
_FILLER = {"the", "a", "an", "of", "in", "on", "at", "to", "and", "with", "for", "from",
           "by", "is", "are", "this", "that", "over", "into", "near"}
# Words that describe the *shot*, not the subject (do not count as topical match).
_STYLE = {"close", "up", "closeup", "slow", "motion", "cinematic", "footage", "video",
          "clip", "shot", "view", "background", "hd", "4k", "roll"}

# Two kinds of mix-up. Extend both lists whenever you spot one in the logs.
#
# SOUNDALIKE: the subject word is unambiguous, but Pexels returns a similar-looking
# word. A reject word only counts when the subject word is missing from the title, so
# "screwdriver repairing a car" is kept but "car driver" is dropped.
SOUNDALIKE = {
    "screwdriver": ("car", "cars", "taxi", "bus", "truck", "racing", "race", "uber",
                    "driving", "license"),
}
# AMBIGUOUS: the subject word itself has two meanings, so the title can contain it and
# still be wrong ("cricket insect on leaf"). Reject words always count here.
AMBIGUOUS = {
    "python": ("snake", "snakes", "reptile"),
    "cricket": ("insect", "insects", "grasshopper", "bug", "chirping", "cicada"),
}

# Per-channel words that must never appear in a clip title (unless the query itself uses them).
CHANNEL_REJECT = {
    "cricket": ("football", "soccer", "basketball", "tennis", "baseball"),
}


def enabled(channel="english"):
    if os.getenv("SEMANTIC_CLIPS", "1").strip() == "0":
        return False
    return os.getenv(f"SEMANTIC_CLIPS_{str(channel).upper()}", "1").strip() != "0"


def _tokens(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def stem(w):
    """Tiny suffix stripper. Applied to both sides, so it only has to be consistent."""
    w = (w or "").lower()
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def slug_title(url):
    """https://www.pexels.com/video/a-wifi-router-3045678/ -> 'a wifi router'"""
    m = re.search(r"/video/([^/]+)/?$", url or "")
    if not m:
        return ""
    return " ".join(w for w in m.group(1).split("-") if not w.isdigit())


def _query_words(query):
    out, seen = [], set()
    for w in _tokens(query):
        if w in _FILLER or w in _STYLE or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def compact_query(text, max_words=5):
    """Drop filler words BEFORE trimming, so the noun is not cut off by the word limit."""
    words = [w for w in _tokens(text) if w not in _FILLER]
    return " ".join(words[:max_words])


def broaden_query(text):
    """Retry query: the two longest subject words, in their original order.
    (The old retry kept the first 2 words, which could be just 'close up'.)"""
    words = _query_words(text)
    if not words:
        return ""
    keep = set(sorted(range(len(words)), key=lambda i: -len(words[i]))[:2])
    return " ".join(w for i, w in enumerate(words) if i in keep)


def score_title(query, title, extra_reject=()):
    """Return (relevance 0-1, rejected). relevance = share of the query's subject
    words found in the title. rejected = title shows the wrong meaning."""
    qw = _query_words(query)
    if not qw:
        return 0.0, False
    tw = _tokens(title)
    t_stems = {stem(w) for w in tw}
    joined = {stem(a + b) for a, b in zip(tw, tw[1:])}          # "screw driver" -> screwdriver
    have = t_stems | joined

    hit = {i for i, w in enumerate(qw) if stem(w) in have}
    for i in range(len(qw) - 1):                                # "wi fi" in query -> wifi in title
        if stem(qw[i] + qw[i + 1]) in have:
            hit.update((i, i + 1))
    relevance = len(hit) / len(qw)

    q_stems = {stem(w) for w in qw}
    reject = {stem(w) for w in extra_reject}
    for key, words in SOUNDALIKE.items():
        if stem(key) in q_stems and stem(key) not in have:
            reject |= {stem(w) for w in words}
    for key, words in AMBIGUOUS.items():
        if stem(key) in q_stems:
            reject |= {stem(w) for w in words}
    rejected = bool(t_stems & (reject - q_stems))
    return relevance, rejected


def _quality(c, min_duration, max_duration):
    """0..1.5 from metadata only (portrait, resolution, duration window)."""
    w, h = c.get("width") or 0, c.get("height") or 0
    short = min(w, h) if w and h else 0
    q = 0.5 if h > w else 0.0
    q += 0.5 if short >= 1080 else 0.25 if short >= 720 else 0.0
    d = c.get("duration") or 0
    lo = min_duration or 0
    hi = max_duration or 10 ** 6
    q += 0.5 if lo <= d <= hi else 0.0
    return q


def rank_for_query(query, candidates, min_duration=None, max_duration=None,
                   exclude_ids=None, extra_reject=(), require_match=True):
    """Rank normalized Pexels candidates ({"id","url","width","height","duration","files"}).
    Returns a best-first list (copies with "relevance"/"score" added). With
    require_match=True a clip whose title shares no subject word with the query,
    or shows the wrong meaning, is dropped, so [] means "nothing relevant here"."""
    exclude = {str(x) for x in (exclude_ids or [])}
    n = max(len(candidates), 1)
    scored = []
    for i, c in enumerate(candidates):
        if str(c.get("id")) in exclude:
            continue
        d = c.get("duration") or 0
        if d and ((min_duration and d < min_duration) or (max_duration and d > max_duration)):
            continue
        rel, rejected = score_title(query, slug_title(c.get("url", "")), extra_reject)
        if rejected or (require_match and rel <= 0):
            continue
        order_bonus = 0.5 * (1 - i / n)                          # Pexels' own order breaks ties
        score = rel * 5.0 + order_bonus + _quality(c, min_duration, max_duration)
        scored.append(dict(c, relevance=rel, score=score))
    scored.sort(key=lambda c: -c["score"])
    if scored:
        top = scored[0]
        print(f"[clip_ranker] '{query}' -> id={top['id']} rel={top['relevance']:.2f} "
              f"title='{slug_title(top.get('url', ''))}' ({len(scored)}/{len(candidates)} passed)")
    else:
        print(f"[clip_ranker] '{query}' -> no relevant clip among {len(candidates)}")
    return scored
EOF_RANKER

cat > tests/test_clip_ranker.py << 'EOF_TESTS'
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
EOF_TESTS

cat > /tmp/patch_semantic_clips.py << 'EOF_PATCH'
"""Applies the semantic-clips patch. Every replacement must match exactly once in
its file, otherwise NOTHING is written to any file (safe to re-run: already-patched
files are skipped)."""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")


JOBS = []


def patch(rel, pairs, marker):
    JOBS.append((rel, pairs, marker))


def apply_all():
    """Two passes: verify EVERY file first, write only if all of them match."""
    todo = []
    for rel, pairs, marker in JOBS:
        src = (ROOT / rel).read_text()
        if marker in src:
            print(f"skip    {rel} (already patched)")
            continue
        for old, _ in pairs:
            n = src.count(old)
            if n != 1:
                sys.exit(f"ABORT: nothing was changed. {rel}: expected 1 match, found {n} for:\n{old[:200]}")
        for old, new in pairs:
            src = src.replace(old, new)
        todo.append((rel, src))
    for rel, src in todo:
        (ROOT / rel).write_text(src)
        print(f"patched {rel}")


# ───────────────────────── English (also used by Bhakti) ─────────────────────────
ENG_SEARCH_OLD = '''def search_pexels_video(query, min_duration=3, max_duration=25):
    pexels_key = os.getenv("PEXELS_API_KEY", "")
    if not pexels_key:
        return None
    headers = {"Authorization": pexels_key}
    params = {
        "query": query,
        "orientation": "portrait",
        "size": "medium",
        "per_page": 1,
        "min_duration": min_duration,
        "max_duration": max_duration,
    }
    r = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    videos = data.get("videos", [])
    if not videos:
        return None
    return _pick_best_video_file(videos[0].get("video_files", []))
'''

ENG_SEARCH_NEW = '''def _ranker_on(channel):
    """Semantic clip ranking on? Never raises; any problem means 'off' (old behaviour)."""
    try:
        from agents import clip_ranker
        return clip_ranker.enabled(channel)
    except Exception:
        return False


def search_pexels_video(query, min_duration=3, max_duration=25, channel="english", used_ids=None):
    pexels_key = os.getenv("PEXELS_API_KEY", "")
    if not pexels_key:
        return None
    use_rank = _ranker_on(channel)
    headers = {"Authorization": pexels_key}
    params = {
        "query": query,
        "orientation": "portrait",
        "size": "medium",
        # semantic ranking needs a pool; still ONE Pexels request per clip
        "per_page": 15 if use_rank else 1,
        "min_duration": min_duration,
        "max_duration": max_duration,
    }
    r = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    videos = data.get("videos", [])
    if not videos:
        return None
    if not use_rank:
        return _pick_best_video_file(videos[0].get("video_files", []))
    try:
        from agents import clip_ranker
        cands = [{"id": v.get("id"), "url": v.get("url", ""), "width": v.get("width") or 0,
                  "height": v.get("height") or 0, "duration": v.get("duration") or 0,
                  "files": v.get("video_files", [])} for v in videos if v.get("id")]
        ranked = clip_ranker.rank_for_query(
            query, cands, min_duration, max_duration, exclude_ids=used_ids,
            extra_reject=clip_ranker.CHANNEL_REJECT.get(channel, ()))
    except Exception as e:  # ranker bug must never block a render: use the old pick
        print(f"  clip_ranker failed ({e}) - using first Pexels result")
        return _pick_best_video_file(videos[0].get("video_files", []))
    if not ranked:
        return None  # nothing relevant: caller retries with a broader query
    if used_ids is not None:
        used_ids.add(ranked[0]["id"])
    return _pick_best_video_file(ranked[0]["files"])
'''

ENG_DL_OLD = '''def download_video_clip(prompt, output_path):
    clean_prompt = re.sub(r"[^a-zA-Z0-9 ,]", "", prompt).strip()
    search_query = " ".join(clean_prompt.split()[:5])

    video_url = search_pexels_video(search_query)
    if not video_url:
        # Retry once with a shorter, broader query (e.g. just the first 2 words)
        broad_query = " ".join(clean_prompt.split()[:2])
        if broad_query != search_query:
            video_url = search_pexels_video(broad_query)
'''

ENG_DL_NEW = '''def download_video_clip(prompt, output_path, channel="english", used_ids=None):
    clean_prompt = re.sub(r"[^a-zA-Z0-9 ,]", "", prompt).strip()
    search_query = " ".join(clean_prompt.split()[:5])
    # Retry once with a shorter, broader query (e.g. just the first 2 words)
    broad_query = " ".join(clean_prompt.split()[:2])
    if _ranker_on(channel):
        from agents import clip_ranker
        # filler words dropped BEFORE trimming so the noun survives; the retry
        # keeps the two longest subject words instead of possibly just "close up"
        search_query = clip_ranker.compact_query(clean_prompt, 5) or search_query
        broad_query = clip_ranker.broaden_query(clean_prompt) or broad_query

    video_url = search_pexels_video(search_query, channel=channel, used_ids=used_ids)
    if not video_url:
        if broad_query != search_query:
            video_url = search_pexels_video(broad_query, channel=channel, used_ids=used_ids)
'''

ENG_GEN_OLD = '''    clip_paths = []
    errors = []
    for i, prompt in enumerate(prompts):
        output_path = os.path.join(folder, f"{i+1}.mp4")
        try:
            download_video_clip(prompt, output_path)
'''
ENG_GEN_NEW = '''    clip_paths = []
    errors = []
    used_ids = set()  # no clip repeated inside one video
    for i, prompt in enumerate(prompts):
        output_path = os.path.join(folder, f"{i+1}.mp4")
        try:
            download_video_clip(prompt, output_path, channel="english", used_ids=used_ids)
'''
patch("agents/video_clip_agent.py",
      [(ENG_SEARCH_OLD, ENG_SEARCH_NEW), (ENG_DL_OLD, ENG_DL_NEW), (ENG_GEN_OLD, ENG_GEN_NEW)],
      marker="def _ranker_on(")

# ───────────────────────── Bhakti ─────────────────────────
patch("agents_bhakti/image_agent.py", [(
    '''    clip_paths = []
    errors = []
    for i, prompt in enumerate(prompts):
        output_path = os.path.join(folder, f"{i+1}.mp4")
        try:
            download_video_clip(prompt, output_path)
''',
    '''    clip_paths = []
    errors = []
    used_ids = set()  # no clip repeated inside one video
    for i, prompt in enumerate(prompts):
        output_path = os.path.join(folder, f"{i+1}.mp4")
        try:
            download_video_clip(prompt, output_path, channel="bhakti", used_ids=used_ids)
''')], marker='channel="bhakti"')

# ───────────────────────── Hindi (Cricket v2 reuses these two functions) ─────────────────────────
HI_SCORE_OLD = '''def score_candidate(c, query_rank, target_sec):
    q_words = set(c["query"].split())
    relevance = len(q_words & _slug_words(c["url"])) / max(len(q_words), 1)

    score = relevance * 4.0
'''
HI_SCORE_NEW = '''def score_candidate(c, query_rank, target_sec, channel="hindi"):
    q_words = set(c["query"].split())
    relevance = len(q_words & _slug_words(c["url"])) / max(len(q_words), 1)

    penalty = 0.0
    try:  # shared semantic scorer; any problem keeps the old relevance above
        from agents import clip_ranker
        if clip_ranker.enabled(channel):
            relevance, rejected = clip_ranker.score_title(
                c["query"], clip_ranker.slug_title(c["url"]),
                clip_ranker.CHANNEL_REJECT.get(channel, ()))
            if rejected:  # wrong meaning (e.g. "car driver" for "screwdriver"): push to the bottom
                penalty = 5.0
    except Exception:
        pass

    score = relevance * 4.0 - penalty
'''
HI_RANK_OLD = '''def rank_candidates(per_query_results, banned_ids, target_sec):
'''
HI_RANK_NEW = '''def rank_candidates(per_query_results, banned_ids, target_sec, channel="hindi"):
'''
HI_RANK_CALL_OLD = '''            s = score_candidate(c, rank, target_sec)
'''
HI_RANK_CALL_NEW = '''            s = score_candidate(c, rank, target_sec, channel)
'''
patch("agents_hindi/video_clip_agent.py",
      [(HI_SCORE_OLD, HI_SCORE_NEW), (HI_RANK_OLD, HI_RANK_NEW), (HI_RANK_CALL_OLD, HI_RANK_CALL_NEW)],
      marker="shared semantic scorer")

# ───────────────────────── Cricket ─────────────────────────
patch("agents_cricket/video_clip_agent.py", [(
    "    ranked = rank_candidates(pools, banned_ids, target_sec)\n",
    '    ranked = rank_candidates(pools, banned_ids, target_sec, channel="cricket")\n')],
    marker='channel="cricket"')

apply_all()
print("done")
EOF_PATCH

python3 /tmp/patch_semantic_clips.py .
echo
python3 -m pytest tests/test_clip_ranker.py tests/test_hindi_categories.py -q || echo "(if pytest is missing: pip install pytest)"
