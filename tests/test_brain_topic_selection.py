"""
Offline tests for the Brain platform integration: agents/brain_client.py, agents/topic_selection.py,
the topic_requests.author_channel_id plumbing. No network, no YouTube, no LLM.
Run:  python -m unittest tests.test_brain_topic_selection -v
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests import comment_helpers as T   # sets up a temp SQLite DB before agents.database loads
from agents import brain_client as bc
from agents import topic_selection as ts
from agents.database import db

CH = "11111111-1111-4111-8111-111111111111"
WS = "22222222-2222-4222-8222-222222222222"
EXEC = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
VER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BRAIN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class Resp:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code, self._body, self._raw = status, body, raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._body


def engine_body(scores, source="brains", extra=()):
    ranked = [{"candidate_id": c, "aggregate_score": s, "ranks": {BRAIN: i + 1}, "reason_codes": []}
              for i, (c, s) in enumerate(sorted(scores.items(), key=lambda kv: -kv[1]))] + list(extra)
    return {"execution_id": EXEC, "source": source, "policy_version": "rrf-v1", "ranked": ranked,
            "brains": [{"brain_id": BRAIN, "brain_version_id": VER, "role": "primary", "status": "ok"}]}


class Env:
    """Context manager that sets a clean Brain env."""
    def __init__(self, **kw):
        base = {"BRAIN_ENGINE_URL": "http://engine.test", "BRAIN_WORKSPACE_ID": WS,
                "BRAIN_CHANNEL_ID_ENGLISH": CH}
        base.update(kw)
        self.env = base

    def __enter__(self):
        self.p = mock.patch.dict(os.environ, {k: v for k, v in self.env.items() if v is not None}, clear=False)
        self.p.start()
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
        os.environ.pop("BRAIN_ENABLED", None) if "BRAIN_ENABLED" not in self.env else None
        return self

    def __exit__(self, *a):
        self.p.stop()


def no_call(*a, **k):
    raise AssertionError("engine must not be called")


class ClientTests(unittest.TestCase):
    def test_request_shape_and_applied_ordering(self):
        calls = []

        def post(url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers, timeout))
            return Resp(200, engine_body({"t2": 0.03, "t0": 0.02, "t1": 0.01}))
        with Env():
            r = bc.rank_topics(["a", "b", "c"], "english", post=post)
        url, body, headers, timeout = calls[0]
        self.assertEqual(url, "http://engine.test/v1/runtime/execute")
        self.assertEqual(headers, {"x-workspace-id": WS})
        self.assertEqual(body["channel_id"], CH)
        self.assertEqual(body["candidates"], [{"candidate_id": "t0", "title": "a"},
                                              {"candidate_id": "t1", "title": "b"},
                                              {"candidate_id": "t2", "title": "c"}])
        self.assertEqual(timeout, (2.0, 2.5))
        self.assertTrue(r.applied)
        self.assertEqual((r.source, r.ordered, r.execution_id, r.policy_version), ("brains", ["c", "a", "b"], EXEC, "rrf-v1"))
        self.assertEqual(r.brain_versions[0]["brain_version_id"], VER)

    def test_off_paths_make_no_call(self):
        cases = [({"BRAIN_ENGINE_URL": ""}, "engine_url_not_set"), ({"BRAIN_ENABLED": "0"}, "kill_switch"),
                 ({"BRAIN_CHANNEL_ID_ENGLISH": ""}, "channel_not_linked"),
                 ({"BRAIN_CHANNEL_ID_ENGLISH": "nope"}, "channel_not_linked"),
                 ({"BRAIN_WORKSPACE_ID": ""}, "workspace_not_set")]
        for env, reason in cases:
            with self.subTest(reason), Env(**env):
                r = bc.rank_topics(["a", "b"], "english", post=no_call)
                self.assertEqual((r.source, r.applied, r.reason), ("disabled", False, reason))
                self.assertFalse(bc.is_configured("english"))
        with Env():
            self.assertTrue(bc.is_configured("english"))
            self.assertFalse(bc.is_configured("hindi"))           # other channels aren't linked

    def test_fewer_than_two_candidates_skips(self):
        with Env():
            self.assertFalse(bc.rank_topics(["a"], "english", post=no_call).applied)
            self.assertFalse(bc.rank_topics(["a", "a"], "english", post=no_call).applied)

    def test_equal_scores_are_no_signal(self):
        with Env():
            r = bc.rank_topics(["a", "b", "c"], "english",
                               post=lambda *a, **k: Resp(200, engine_body({"t0": 0.0, "t1": 0.0, "t2": 0.0})))
        self.assertEqual((r.source, r.applied, r.reason), ("brains", False, "no_signal"))

    def test_engine_baseline_fallback_reported_honestly(self):
        with Env():
            r = bc.rank_topics(["a", "b"], "english",
                               post=lambda *a, **k: Resp(200, engine_body({}, source="baseline_fallback")))
        self.assertEqual((r.source, r.applied, r.reason), ("baseline_fallback", False, "engine_baseline_fallback"))

    def test_partial_applied_and_unknown_ids_ignored(self):
        extra = [{"candidate_id": "evil", "aggregate_score": 9.0, "ranks": {}, "reason_codes": []}]
        with Env():
            r = bc.rank_topics(["a", "b", "c"], "english", post=lambda *a, **k: Resp(
                200, engine_body({"t2": 0.03, "t1": 0.02}, source="partial", extra=extra)))
        self.assertEqual((r.source, r.applied, r.ordered), ("partial", True, ["c", "b", "a"]))

    def test_every_failure_mode_degrades_without_raising(self):
        import requests
        bad = [lambda *a, **k: Resp(503, {}), lambda *a, **k: Resp(200, raw="<html>"),
               lambda *a, **k: Resp(200, {"hello": "world"}),
               lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("refused")),
               lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow"))]
        for post in bad:
            with Env():
                r = bc.rank_topics(["a", "b"], "english", post=post)
            self.assertEqual((r.source, r.applied), ("engine_unavailable", False))
            self.assertTrue(r.reason.startswith("call_failed"))

    def test_features_forwarded_and_cleaned(self):
        sent = {}

        def post(url, json=None, **k):
            sent.update(json=json)
            return Resp(200, engine_body({"t1": 0.03, "t0": 0.02}))
        feats = {"a": {"unique_requesters": 12, "similar_requests": 30, "days_since_trend": 1.5, "junk": 1},
                 "b": {"unique_requesters": -4, "days_since_trend": "yesterday", "similar_requests": True}}
        with Env():
            bc.rank_topics(["a", "b"], "english", features=feats, post=post)
        c0, c1 = sent["json"]["candidates"]
        self.assertEqual(c0["features"], {"unique_requesters": 12, "similar_requests": 30, "days_since_trend": 1.5})
        self.assertNotIn("features", c1)


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def req(topic, author, days_ago=1, comment=""):
    return {"topic": topic, "comment": comment, "author_channel_id": author,
            "timestamp": (NOW - timedelta(days=days_ago)).isoformat()}


class DemandFeatureTests(unittest.TestCase):
    CANDS = [{"title": "Quantum computing breakthrough explained", "age_days": 1.0},
             {"title": "New GPU launch review", "age_days": 3.0},
             {"title": "Mystery title", "age_days": None}]

    def test_unique_requesters_counts_distinct_known_people_only(self):
        rows = [req("quantum computing", "UC_A"), req("quantum computing", "UC_A"),   # same person twice
                req("quantum computing", "UC_B"), req("quantum computing", None)]       # unknown author
        f = ts.demand_features(self.CANDS, rows, now=NOW)
        q = f["Quantum computing breakthrough explained"]
        self.assertEqual((q["unique_requesters"], q["similar_requests"]), (2, 4))
        self.assertEqual(q["days_since_trend"], 1.0)

    def test_unrequested_candidates_get_freshness_only_and_old_requests_ignored(self):
        f = ts.demand_features(self.CANDS, [req("quantum computing", "UC_A", days_ago=45)], now=NOW)
        self.assertEqual(f["New GPU launch review"], {"days_since_trend": 3.0})
        self.assertEqual(f["Quantum computing breakthrough explained"], {"days_since_trend": 1.0})
        self.assertEqual(f["Mystery title"], {})

    def test_garbage_rows_are_skipped(self):
        f = ts.demand_features(self.CANDS, [{"topic": "x", "timestamp": "not a date"}, {}], now=NOW)
        self.assertEqual(f["New GPU launch review"], {"days_since_trend": 3.0})


class SelectionTests(unittest.TestCase):
    CANDS = [{"title": "alpha news", "age_days": 2.0}, {"title": "beta chip", "age_days": 1.0},
             {"title": "gamma robot", "age_days": 0.5}]

    def run_sel(self, rank_result, *, sat=None, candidates=None, recent=(), requests=None, env=None):
        used, sat_seen, logs = [], [], []

        def check(topic):
            sat_seen.append(topic)
            ok = (sat or {}).get(topic, True)
            return {"proceed": ok, "opportunity_score": 80 if ok else 10, "reason": "test"}
        with Env(**(env or {})):
            out = ts.brain_ranked_topic(
                "english", "US", recent_topics=recent, log=logs.append,
                get_candidates=lambda region: list(candidates if candidates is not None else self.CANDS),
                get_requests=(requests if callable(requests) else (lambda: requests or [])),
                check_saturation=check, mark_used=used.append, rank=lambda *a, **k: rank_result)
        return out, used, sat_seen, logs

    def ok(self, ordered):
        return bc.BrainResult(source="brains", applied=True, reason="ok", ordered=ordered, execution_id=EXEC,
                              policy_version="rrf-v1", brain_versions=[{"brain_version_id": VER}], topics_sent=3)

    def test_feature_off_does_no_work(self):
        def boom(*a, **k):
            raise AssertionError("must not run")
        with Env(BRAIN_ENGINE_URL=""):
            self.assertIsNone(ts.brain_ranked_topic("english", get_candidates=boom, get_requests=boom,
                                                    check_saturation=boom, rank=boom))

    def test_brain_top_is_chosen_and_marked_used(self):
        out, used, seen, logs = self.run_sel(self.ok(["gamma robot", "beta chip", "alpha news"]))
        self.assertEqual((out, used, seen), ("gamma robot", ["gamma robot"], ["gamma robot"]))
        line = next(l for l in logs if l.startswith("BRAIN_LOG "))
        j = json.loads(line[len("BRAIN_LOG "):])
        self.assertEqual((j["source"], j["applied"], j["execution_id"], j["policy_version"]),
                         ("brains", True, EXEC, "rrf-v1"))

    def test_saturated_top_falls_to_next_in_brain_order(self):
        out, used, seen, _ = self.run_sel(self.ok(["gamma robot", "beta chip", "alpha news"]),
                                          sat={"gamma robot": False})
        self.assertEqual((out, seen, used), ("beta chip", ["gamma robot", "beta chip"], ["beta chip"]))

    def test_all_saturated_returns_none_and_marks_nothing(self):
        out, used, *_ = self.run_sel(self.ok(["gamma robot", "beta chip", "alpha news"]),
                                     sat={"gamma robot": False, "beta chip": False, "alpha news": False})
        self.assertIsNone(out)
        self.assertEqual(used, [])

    def test_not_applied_returns_none_without_saturation_calls(self):
        for res in (bc.BrainResult(source="engine_unavailable", applied=False, reason="call_failed:ConnectError"),
                    bc.BrainResult(source="brains", applied=False, reason="no_signal"),
                    bc.BrainResult(source="baseline_fallback", applied=False, reason="engine_baseline_fallback")):
            out, used, seen, logs = self.run_sel(res)
            self.assertIsNone(out)
            self.assertEqual((used, seen), ([], []))
            self.assertTrue(any("BRAIN_LOG" in l and res.source in l for l in logs))   # source never hidden

    def test_recently_posted_topics_are_never_sent_for_ranking(self):
        sent = {}

        def rank(topics, channel, features=None):
            sent["topics"] = list(topics)
            return self.ok([t for t in topics])
        with Env():
            out = ts.brain_ranked_topic("english", recent_topics=["beta chip"], log=lambda *_: None,
                                        get_candidates=lambda r: list(self.CANDS), get_requests=lambda: [],
                                        check_saturation=lambda t: {"proceed": True, "opportunity_score": 1, "reason": ""},
                                        mark_used=lambda t: None, rank=rank)
        self.assertNotIn("beta chip", sent["topics"])
        self.assertEqual(out, "alpha news")

    def test_too_few_candidates_returns_none(self):
        out, *_ = self.run_sel(self.ok([]), candidates=[self.CANDS[0]])
        self.assertIsNone(out)

    def test_signal_store_failure_still_ranks_on_freshness(self):
        captured = {}

        def rank(topics, channel, features=None):
            captured["features"] = features
            return self.ok(list(topics))

        def boom():
            raise RuntimeError("db down")
        with Env():
            out = ts.brain_ranked_topic("english", log=lambda *_: None, get_candidates=lambda r: list(self.CANDS),
                                        get_requests=boom, check_saturation=lambda t: {"proceed": True, "opportunity_score": 1, "reason": ""},
                                        mark_used=lambda t: None, rank=rank)
        self.assertEqual(out, "alpha news")
        self.assertEqual(captured["features"]["gamma robot"], {"days_since_trend": 0.5})


class AuthorPlumbingTests(unittest.TestCase):
    def setUp(self):
        T.reset_db()

    def test_comment_engine_stores_author_on_topic_requests(self):
        llm = T.FakeLLM(classify={"intent": "suggestion", "language": "english", "reply_strategy": "acknowledge",
                                  "confidence": 0.9, "requested_topic": "quantum chips explained"},
                        generate=[{"reply_text": "noted bro", "strategy": "acknowledge"}])
        svc, *_ = T.make_service("english", [T.thread("c1", "make a video on quantum chips", author="UC_FAN")],
                                 llm=llm, real_topic_sink=True)      # real sink -> agents.database.db
        svc.sync()
        svc.process_pending()
        rows = db.get_topic_requests("english")
        self.assertEqual([(r["topic"], r["author_channel_id"]) for r in rows], [("quantum chips explained", "UC_FAN")])

    def test_save_topic_request_without_author_still_works(self):
        db.save_topic_request("english", "robots", "pls", "vid1")
        self.assertIsNone(db.get_topic_requests("english")[0]["author_channel_id"])

    def test_old_database_without_the_column_is_migrated(self):
        import sqlite3
        import tempfile
        from agents.database import Database
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE topic_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT DEFAULT 'english', "
                     "topic TEXT, comment TEXT, video_id TEXT, timestamp TEXT)")
        conn.execute("INSERT INTO topic_requests (topic) VALUES ('legacy')")
        conn.commit(); conn.close()
        d = Database(db_path=path)
        d.save_topic_request("english", "new", "c", "v", author_channel_id="UC_X")
        rows = {r["topic"]: r["author_channel_id"] for r in d.get_topic_requests("english")}
        self.assertEqual(rows, {"legacy": None, "new": "UC_X"})
        Database(db_path=path)                                   # second startup: migration is a no-op


if __name__ == "__main__":
    unittest.main()
