# agents_horror/video_agent.py
"""Stage 9: render. Same ffmpeg/imageio-ffmpeg toolchain and frame size as every other channel
(agents.video_agent.get_ffmpeg, SHORTS_WIDTH/HEIGHT, assets/fonts), but each clip is cut to ITS scene's
real narration length instead of an equal split, and sound design is mixed under the voice.
Vertical 9:16, 30 fps, H.264/AAC MP4. Light, consistent grade; plain cuts or short dips to black."""
import os
import re
import subprocess

from agents.video_agent import get_ffmpeg, SHORTS_WIDTH, SHORTS_HEIGHT, FONTS_DIR

FPS = 30
GRADE = "eq=saturation=0.78:contrast=1.08:brightness=-0.04,vignette=PI/5"
BED_VOLUME, SFX_VOLUME = 0.16, 0.55
CAPTION_STYLE = ("FontName=Hind,FontSize=64,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,"
                 "BorderStyle=1,Outline=4,Shadow=0,Alignment=2,MarginV=330,Bold=1")


class RenderError(RuntimeError):
    pass


def _ts(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(scenes, path="output/horror_captions.srt", chunk=4):
    """Captions timed from the real per-scene durations (not an even split over the whole video)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines, n = [], 1
    for sc in scenes:
        words = re.sub(r"\.\.\.|…", " ", sc.narration).split()
        speak = max(sc.duration - sc.pause_after, 0.5)
        groups = [words[i:i + chunk] for i in range(0, len(words), chunk)]
        total = sum(len(g) for g in groups) or 1
        t = sc.start
        for g in groups:
            d = speak * len(g) / total
            lines.append(f"{n}\n{_ts(t)} --> {_ts(t + d)}\n{' '.join(g)}\n")
            t += d
            n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RenderError(f"{what} failed: {r.stderr[-400:]}")
    return r


def _segment(ffmpeg, clip, scene, idx, width, height, work):
    out = os.path.join(work, f"seg_{idx:03d}.mp4")
    vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps={FPS},{GRADE}"
    if scene.transition == "fade" and scene.duration > 1.0:
        vf += f",fade=t=in:st=0:d=0.25,fade=t=out:st={scene.duration - 0.25:.2f}:d=0.25"
    _run([ffmpeg, "-y", "-stream_loop", "-1", "-i", clip, "-t", f"{scene.duration:.3f}", "-vf", vf, "-an",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", out],
         f"segment {scene.scene_id}")
    return out


def render_video(scenes, assets, voice_path, sound, subtitles=True, out_path="output/horror_final.mp4",
                 width=SHORTS_WIDTH, height=SHORTS_HEIGHT, work="output/horror_work"):
    """Returns {"path", "duration"}. Raises RenderError if a scene has no usable clip
    (the scheduler/reviewer flag that BEFORE calling this)."""
    ffmpeg = get_ffmpeg()
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    segs = []
    for i, sc in enumerate(scenes):
        a = assets.get(sc.scene_id) or {}
        if a.get("status") not in ("ok", "reused_same_location") or not os.path.exists(a.get("path", "")):
            raise RenderError(f"scene {sc.scene_id} has no usable clip")
        segs.append(_segment(ffmpeg, a["path"], sc, i, width, height, work))
    concat_txt = os.path.join(work, "concat.txt")
    with open(concat_txt, "w") as f:
        f.writelines(f"file '{os.path.abspath(s)}'\n" for s in segs)
    silent = os.path.join(work, "bg_silent.mp4")
    _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", silent], "concat")

    total = sum(sc.duration for sc in scenes)
    inputs = ["-i", silent, "-i", voice_path]
    chains, labels = ["[1:a]volume=1.0[voice]"], ["[voice]"]
    n_in = 2
    if sound and sound.get("bed"):
        inputs += ["-i", sound["bed"]["path"]]
        chains.append(f"[{n_in}:a]aloop=loop=-1:size=2000000000,atrim=0:{total:.2f},volume={BED_VOLUME}[bed]")
        labels.append("[bed]")
        n_in += 1
    for k, cue in enumerate((sound or {}).get("sfx", [])):
        inputs += ["-i", cue["path"]]
        ms = int(cue["at"] * 1000)
        chains.append(f"[{n_in}:a]atrim=0:3,afade=t=out:st=2.4:d=0.6,adelay={ms}|{ms},volume={SFX_VOLUME}[sfx{k}]")
        labels.append(f"[sfx{k}]")
        n_in += 1
    if len(labels) > 1:  # amix lowers every input by 1/N: scale back so the voice stays clear
        chains.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:dropout_transition=0,volume={len(labels)}[aout]")
    else:
        chains.append("[voice]anull[aout]")

    def _final(with_subs):
        vchain = "[0:v]null[vout]"
        if with_subs:
            srt = build_srt(scenes)
            vchain = f"[0:v]subtitles={srt}:fontsdir={FONTS_DIR}:force_style='{CAPTION_STYLE}'[vout]"
        cmd = [ffmpeg, "-y", *inputs, "-filter_complex", ";".join([vchain, *chains]),
               "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.2f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path]
        _run(cmd, "final render")

    try:
        _final(subtitles)
    except RenderError as e:
        if not subtitles:
            raise
        print(f"[horror render] caption burn failed, retrying without captions: {e}")
        _final(False)
    return {"path": out_path, "duration": total}
