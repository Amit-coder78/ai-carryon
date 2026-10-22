"""Verified-data charts: only ever drawn from a Fact.series. Needs matplotlib + ffmpeg (ffmpeg is already used by the renderer)."""
import subprocess


class ChartUnavailable(RuntimeError):
    pass


def render_chart_png(scene_chart, out_png):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise ChartUnavailable("matplotlib not installed (see requirements_finance.txt)") from e
    labels = [p[0] for p in scene_chart["series"]]
    vals = [float(p[1]) for p in scene_chart["series"]]
    fig, ax = plt.subplots(figsize=(10.8, 19.2), dpi=100)
    fig.patch.set_facecolor("#0b1220"); ax.set_facecolor("#0b1220")
    ax.plot(range(len(vals)), vals, color="#3ddc97", linewidth=5)
    ax.fill_between(range(len(vals)), vals, min(vals), color="#3ddc97", alpha=0.12)
    step = max(1, len(labels) // 5)
    ax.set_xticks(range(0, len(labels), step)); ax.set_xticklabels([l[5:] for l in labels[::step]], color="#9fb3c8", fontsize=22)
    ax.tick_params(axis="y", colors="#9fb3c8", labelsize=22)
    for sp in ax.spines.values(): sp.set_visible(False)
    ax.grid(axis="y", color="#1f2b44")
    fig.text(0.5, 0.90, scene_chart["title"], ha="center", color="white", fontsize=34, weight="bold", wrap=True)
    fig.text(0.5, 0.08, f"Source: {scene_chart['source']}  |  Data as of {scene_chart['as_of'][:10]}", ha="center", color="#9fb3c8", fontsize=20, wrap=True)
    fig.subplots_adjust(left=0.14, right=0.95, top=0.85, bottom=0.18)
    fig.savefig(out_png, facecolor=fig.get_facecolor()); plt.close(fig)
    return out_png


def png_to_clip(png, out_mp4, seconds):
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", png, "-t", f"{max(seconds, 1.0):.1f}", "-r", "30",
           "-vf", "scale=1080:1920,format=yuv420p", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_mp4]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0: raise RuntimeError("ffmpeg failed: " + r.stderr[-300:])
    return out_mp4
