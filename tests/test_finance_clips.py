"""Finance clip reuse: python -m unittest tests.test_finance_clips -v"""
import os, sys, tempfile, types, unittest

from agents_finance import media, scene_planner


def _fake_module(pool):
    """Fake agents.video_clip_agent: each query has a pool of ids; honours used_ids like the real one."""
    calls = []
    def download_video_clip(prompt, out, channel="english", used_ids=None):
        for cid in pool.get(prompt, []):
            if used_ids is None or cid not in used_ids:
                if used_ids is not None: used_ids.add(cid)
                open(out, "wb").write(b"x"); calls.append((prompt, cid)); return out
        raise RuntimeError(f"No Pexels video found for query: '{prompt}'")
    m = types.ModuleType("agents.video_clip_agent"); m.download_video_clip = download_video_clip
    return m, calls


class Store:
    def __init__(self): self.d = {}
    def get_meta(self, k): return self.d.get(k)
    def set_meta(self, k, v): self.d[k] = v


class FinanceClipReuse(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("agents.video_clip_agent")
        os.environ.pop("FINANCE_CLIP_COOLDOWN_DAYS", None)
    def tearDown(self):
        if self._saved is not None: sys.modules["agents.video_clip_agent"] = self._saved
        else: sys.modules.pop("agents.video_clip_agent", None)

    def _run(self, store, scenes):
        with tempfile.TemporaryDirectory() as d:
            return media.fetch_scene_media(scenes, out_dir=os.path.join(d, "c"), store=store)

    def _scenes(self):
        return [scene_planner.Scene(0, "a", 5, "stock", ["nifty chart"]),
                scene_planner.Scene(1, "b", 5, "stock", ["nifty chart"])]

    def test_second_video_gets_different_clips(self):
        mod, calls = _fake_module({"nifty chart": [1, 2, 3, 4], **{q: [] for q in media.GENERIC_QUERIES}})
        sys.modules["agents.video_clip_agent"] = mod
        store = Store()
        self._run(store, self._scenes()); first = {c[1] for c in calls}; calls.clear()
        self._run(store, self._scenes()); second = {c[1] for c in calls}
        self.assertEqual(len(first), 2); self.assertEqual(len(second), 2)
        self.assertFalse(first & second)                      # no clip shared between the two videos

    def test_same_video_never_repeats_a_clip(self):
        mod, calls = _fake_module({"nifty chart": [1, 2, 3]})
        sys.modules["agents.video_clip_agent"] = mod
        self._run(Store(), self._scenes())
        self.assertEqual(len({c[1] for c in calls}), 2)

    def test_cooldown_expires(self):
        mod, calls = _fake_module({"nifty chart": [1, 2]})
        sys.modules["agents.video_clip_agent"] = mod
        store = Store(); now = [1_000_000.0]
        mem = media.ClipMemory(store.get_meta, store.set_meta, now=lambda: now[0])
        mem.add([1, 2])
        self.assertEqual(mem.recent_ids(), {1, 2})
        now[0] += 15 * 86400
        self.assertEqual(mem.recent_ids(), set())             # 14-day window passed

    def test_exhausted_pool_relaxes_instead_of_failing(self):
        mod, calls = _fake_module({"nifty chart": [1, 2], **{q: [] for q in media.GENERIC_QUERIES}})
        sys.modules["agents.video_clip_agent"] = mod
        store = Store()
        self._run(store, self._scenes()); calls.clear()
        paths, errs = self._run(store, self._scenes())        # every clip is in cooldown now
        self.assertEqual(len(paths), 2); self.assertEqual(errs, [])

    def test_storage_failure_does_not_break_render(self):
        mod, calls = _fake_module({"nifty chart": [1, 2]})
        sys.modules["agents.video_clip_agent"] = mod
        class Broken:
            def get_meta(self, k): raise OSError("db down")
            def set_meta(self, k, v): raise OSError("db down")
        paths, errs = self._run(Broken(), self._scenes())
        self.assertEqual(len(paths), 2)


if __name__ == "__main__":
    unittest.main()
