# AI CarryON Horror (6th pipeline)

Original English horror stories as vertical 9:16 videos: YouTube Shorts (45–60 s, default) or long-form
(90–1200 s). Every video is labelled as **fiction** — never as a true story.

## What it reuses (nothing duplicated)
| Need | Reused from |
|---|---|
| LLM (Groq first, Gemini fallback) | `agents_cricket.script_agent.safe_invoke`, wrapped with a per-run call cap |
| Stock footage (**same as Cricket: Pexels**) | `agents_hindi.video_clip_agent` search / rank / download + `agents.clip_ranker` relevance scoring |
| Key fallback | `key_fallback.install()` (per-channel key → shared key) |
| Resume after a crash | `agents.run_state.RunState` |
| Database | `agents.database.db` with `channel="horror"` (+ `horror_*` meta keys) |
| Narration | `edge-tts` (English channel's engine) + `pydub` (Cricket's) |
| Ambience / SFX | Freesound helpers from `agents_bhakti.music_agent` (CC0 first, CC-BY with attribution) |
| Rendering | `agents.video_agent` ffmpeg helpers, frame size, fonts |
| Upload | same pattern as `agents_cricket.upload_agent`, Horror's own token |
| Dashboard | new "👻 Horror Channel" tab in `pages/1_Dashboard.py` |

No new dependencies (`requirements_cricket.txt` is used by the workflow) and no new paid API.
The repo has no central registry: a pipeline = `scheduler_<name>.py` + `agents_<name>/` + a workflow + a dashboard tab. Horror follows that.

## Stages (`scheduler_horror.py`)
concept → research (optional) → **story bible** → script → scene plan → per-scene narration → Pexels clips →
sound design → render → metadata → review → upload. Each step is saved, so a stopped run resumes without redoing LLM work.
A typical run uses **5 LLM calls** (cap: `HORROR_MAX_LLM_CALLS`, default 8). Narration is rebuilt from the script
(the planner only groups sentences), so scenes can never drop or invent narration.

## Setup
1. Secrets (GitHub → Settings → Secrets): `DATABASE_URL`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `PEXELS_API_KEY` (already exist; optional
   `*_HORROR` versions are preferred when set), `FREESOUND_API_KEY` (optional, for ambience/SFX), `HORROR_YOUTUBE_TOKEN_B64`
   (optional: without it the run renders only). Create the token like `generate_cricket_token.py`, with the Horror channel's Google account.
2. Run locally: `python scheduler_horror.py --no-upload` (needs `ffmpeg`; keys in `.env`).
3. Automatic: `.github/workflows/horror-scheduler.yml` (every 3 h; the daily cap decides). Manual run: Actions → Horror Scheduler → Run workflow.

## Configuration (all `HORROR_*`, repo *variables*; unset = default)
`CATEGORY` (supernatural | psychological | folklore | mystery | creature; empty = rotate), `FORMAT` (shorts | long), `DURATION`,
`INTENSITY` (1–5), `SETTING`, `MODE` (standalone | episodic), `SERIES_ID`, `IDEA`, `MUSIC`, `SUBTITLES`, `RESEARCH`,
`UPLOAD` (0 = dry run), `MAX_LLM_CALLS`, `MAX_SCENE_RETRIES`, `MAX_SFX`, `MIN_RELEVANCE`, `DAILY_UPLOAD_CAP` (2), `MIN_GAP_HOURS` (4),
`EDGE_VOICE` / `EDGE_RATE` / `EDGE_PITCH`, `VEO` (1 = let missing scenes use the repo's Veo helper; off by default).
CLI flags mirror these: `--category --format --duration --intensity --setting --mode --series --idea --no-upload --no-music --no-subtitles --research --force`.

## Media sourcing rules
* A clip is used only if its Pexels title matches the scene's query (relevance gate); wrong-meaning words are rejected.
* No match → retry with broader queries (only that scene) → reuse the clip of an earlier scene in the **same location** (flagged) →
  otherwise the scene is flagged **missing**, nothing is rendered or uploaded, and `output/horror_report.json` lists it.
* Clips live in `assets/horror_clips/` (other pipelines wipe `assets/pexels_clips/`). Clip ids are remembered only after a successful upload.

## Series / continuity
The story bible (characters, clothing, locations, props, entities, lighting, continuity rules) is created before scene planning and every scene
references it. In episodic mode the bible and the last episode summaries are stored in the DB and reused, and titles get `(Ep N)`.

## Tests
`python -m pytest tests/test_horror.py tests/test_horror_pipeline.py` — all mocked (LLM, Pexels, TTS, Freesound); the render test runs real ffmpeg on tiny clips.

## Known limitations
* **Stock footage cannot keep the same actor or location across clips.** Continuity is carried by the bible, by the keywords, and by reusing a
  same-location clip; identity consistency is not promised. Veo (opt-in) is prompted from text only and was **not** tested against the live API.
* Sound effects are matched by keyword from Freesound previews and are only as good as the search result. Without `FREESOUND_API_KEY` there is no ambience.
* Narration quality depends on the free edge-tts voices; per-scene synthesis adds a short gap between scenes.
* Long-form is also rendered 9:16 (no horizontal output) and the dashboard has no Horror analytics page or view tracker yet.
* The reviewer is rule-based (no extra LLM call): it checks structure, timing, assets, labelling and obvious hook/ending problems, not story quality in depth.
* Research is a Wikipedia summary lookup (off by default); it does not verify the script against sources.
