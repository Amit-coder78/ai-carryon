"""Stage 8: media. Stock footage reuses the repo's existing Pexels downloader (agents.video_clip_agent.download_video_clip);
data scenes use charts built from verified series."""
import os, shutil

from . import charts


def _default_clip_fn():
    from agents.video_clip_agent import download_video_clip   # existing shared Pexels integration
    return download_video_clip


def fetch_scene_media(scenes, out_dir="assets/pexels_clips", clip_fn=None, chart_fn=None, png_fn=None):
    """Returns (paths_in_scene_order, errors). A failed chart falls back to GENERIC footage (never entity-specific)."""
    clip_fn = clip_fn or _default_clip_fn()
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
                clip_fn(" ".join(s.search_terms[0].split()[:5]), out)
            paths.append(out)
        except _Fallback:
            try:
                clip_fn("stock market trading screen", out); paths.append(out)
            except Exception as e:
                errors.append(f"scene {s.index} fallback failed: {e}")
        except Exception as e:
            errors.append(f"scene {s.index}: {e}")
    return paths, errors


class _Fallback(Exception):
    pass
