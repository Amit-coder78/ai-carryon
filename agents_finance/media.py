"""Stage 8: media. Stock footage reuses the repo's shared Pexels downloader (agents.video_clip_agent.download_video_clip,
which already ranks clips for relevance with agents/clip_ranker.py). On top of that, Finance remembers every Pexels clip
it used and will not reuse one for FINANCE_CLIP_COOLDOWN_DAYS (default 14, same idea as the hook-engine cooldown), so
two videos never share clips. Memory lives in finance_meta (key finance_used_pexels_clips), nothing shared with other
channels. Data scenes use charts built from verified series."""
import json, os, shutil, time

from . import charts

MEMORY_KEY = "finance_used_pexels_clips"
MAX_REMEMBERED = 500
# Tried in order when a scene's own search terms yield nothing fresh. First entry is also the chart-failure fallback.
GENERIC_QUERIES = ["stock market trading screen", "financial charts monitor", "business meeting office",
                   "calculator and money", "city skyline financial district", "laptop analytics dashboard"]


def cooldown_days():
    try:
        return float(os.getenv("FINANCE_CLIP_COOLDOWN_DAYS", "14"))
    except ValueError:
        return 14.0


class ClipMemory:
    """Clip ids used recently. get_meta/set_meta come from the Finance store. Never raises: a storage problem
    just means 'no memory' (old behaviour), it can never stop a video."""
    def __init__(self, get_meta=None, set_meta=None, now=time.time):
        self.get_meta, self.set_meta, self.now = get_meta, set_meta, now

    def _load(self):
        try:
            raw = self.get_meta(MEMORY_KEY) if self.get_meta else None
            return [e for e in json.loads(raw) if "id" in e and "t" in e] if raw else []
        except Exception as e:
            print(f"Finance clip memory unavailable (load): {e}")
            return []

    def recent_ids(self):
        limit = self.now() - cooldown_days() * 86400
        return {int(e["id"]) for e in self._load() if e["t"] >= limit}

    def add(self, ids):
        if not (ids and self.set_meta):
            return
        try:
            limit = self.now() - cooldown_days() * 86400
            keep = [e for e in self._load() if e["t"] >= limit]          # expired entries drop out here
            keep += [{"id": int(i), "t": self.now()} for i in ids]
            self.set_meta(MEMORY_KEY, json.dumps(keep[-MAX_REMEMBERED:]))
        except Exception as e:
            print(f"Finance clip memory unavailable (save): {e}")


class FreshClipFn:
    """clip_fn(query, out) that skips clips used in this video AND inside the cooldown window.
    relax() drops only the cooldown part (last resort, so a render never fails for lack of fresh clips)."""
    def __init__(self, memory, channel="finance"):
        self.memory, self.channel = memory, channel
        self.cooldown_ids = memory.recent_ids()
        self.in_video = set()
        self.relaxed = False

    def __call__(self, query, out):
        from agents.video_clip_agent import download_video_clip   # shared Pexels integration + clip_ranker
        used = set(self.in_video) if self.relaxed else self.cooldown_ids | self.in_video
        before = set(used)
        try:
            download_video_clip(query, out, channel=self.channel, used_ids=used)
        finally:
            self.in_video |= used - before                          # picked ids count even if the download failed
        return out

    def relax(self):
        self.relaxed = True

    def strict(self):
        self.relaxed = False

    def commit(self):
        self.memory.add(self.in_video)


def _try(clip_fn, queries, out):
    last = None
    for q in queries:
        try:
            clip_fn(q, out)
            return q
        except Exception as e:
            last = e
    raise last or RuntimeError("no query")


def _stock(clip_fn, s, out):
    terms = [" ".join(t.split()[:5]) for t in s.search_terms] + GENERIC_QUERIES
    try:
        return _try(clip_fn, terms, out)
    except Exception:
        if not hasattr(clip_fn, "relax"):
            raise
    print(f"CLIP_REUSE_FALLBACK scene {s.index}: no fresh clip left, allowing a clip from the cooldown window")
    clip_fn.relax()
    try:
        return _try(clip_fn, terms, out)
    finally:
        clip_fn.strict()


def fetch_scene_media(scenes, out_dir="assets/pexels_clips", clip_fn=None, chart_fn=None, png_fn=None, store=None):
    """Returns (paths_in_scene_order, errors). A failed chart falls back to GENERIC footage (never entity-specific).
    Pass store (the Finance store) to get cross-video clip memory; an injected clip_fn is used as-is."""
    fresh = None
    if clip_fn is None:
        fresh = FreshClipFn(ClipMemory(getattr(store, "get_meta", None), getattr(store, "set_meta", None)))
        clip_fn = fresh
    chart_fn = chart_fn or charts.render_chart_png
    png_fn = png_fn or charts.png_to_clip
    shutil.rmtree(out_dir, ignore_errors=True); os.makedirs(out_dir, exist_ok=True)
    paths, errors = [], []
    for s in scenes:
        out = os.path.join(out_dir, f"{len(paths) + 1}.mp4")
        try:
            if s.visual_type == "chart":
                try:
                    png = chart_fn(s.chart, out.replace(".mp4", ".png"))
                    png_fn(png, out, s.duration_sec)
                except Exception as e:
                    errors.append(f"scene {s.index} chart failed ({e}); using generic footage")
                    raise _Fallback()
            else:
                _stock(clip_fn, s, out)
            paths.append(out)
        except _Fallback:
            try:
                fake = type("S", (), {"index": s.index, "search_terms": []})()
                _stock(clip_fn, fake, out); paths.append(out)
            except Exception as e:
                errors.append(f"scene {s.index} fallback failed: {e}")
        except Exception as e:
            errors.append(f"scene {s.index}: {e}")
    if fresh:
        fresh.commit()
    return paths, errors


class _Fallback(Exception):
    pass
