<div align="center">

# AI CarryON

### Autonomous multi-channel YouTube Shorts platform

Finds trending topics, researches them, writes scripts, produces voiceovers, renders videos, and uploads to YouTube with no human in the loop, then learns from how each video performs.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-scheduled%20pipelines-2088FF?logo=githubactions&logoColor=white)
![Postgres](https://img.shields.io/badge/Supabase-Postgres-3ECF8E?logo=supabase&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-dashboard-FF4B4B?logo=streamlit&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-Groq%20%2B%20Gemini-8A2BE2)

[Live dashboard](https://ai-carryon-tqndlmjbcfvtznagmef2ap.streamlit.app/Dashboard) |
[Brain Platform backend](https://github.com/Amit-coder78/ai-carryon-brain-platform) |
[Backend architecture](https://htmlpreview.github.io/?https://github.com/Amit-coder78/ai-carryon-brain-platform/blob/main/docs/architecture/index.html)

</div>

---

## Overview

AI CarryON runs **seven independent pipelines**, each with its own content source, audience, credentials, and schedule, all deployed as scheduled GitHub Actions workflows at zero hosting cost. Five are documented here as production channels; Finance and Horror have their own docs ([`FINANCE.md`](FINANCE.md), [`HORROR.md`](HORROR.md)).

| | |
| --- | --- |
| **Compute** | GitHub Actions cron workflows (stateless runs) |
| **State** | Supabase Postgres, partitioned per channel |
| **LLMs** | Groq `openai/gpt-oss-120b` primary, Gemini fallback, with circuit breakers and call budgets |
| **Decision backend** | [ai-carryon-brain-platform](https://github.com/Amit-coder78/ai-carryon-brain-platform): versioned "Brains" that rank candidate topics |
| **Voice** | Edge TTS, Sarvam AI (Hindi) |
| **Media** | Pexels and Twitch clips, ffmpeg rendering |
| **Dashboard** | Streamlit, reading the same Postgres database |
| **Channels** | [English](https://www.youtube.com/@AIcarryONAI) / [Hindi](https://www.youtube.com/@AICarryONHindi) / [Cricket](https://www.youtube.com/@AICarryONSports) / [Gaming](https://www.youtube.com/@AICarryONGaming) |

### Highlights

- **Fully autonomous**: topic discovery to YouTube upload, with learned peak-hour scheduling.
- **Self-improving**: velocity tracking, saturation scoring, competitor benchmarking, and A/B title testing feed back into every run.
- **Resilient by design**: provider fallback, per-channel API key isolation, resumable runs, and a backend that can never block a video if it fails.
- **Zero-cost infrastructure**: migrated off paid hosting to scheduled workflows plus Postgres.

## Contents

[Channels](#channels-in-production) | [Pipeline](#what-each-pipeline-does) | [Architecture](#architecture) | [Brain Platform backend](#brain-platform-backend) | [Intelligence layer](#intelligence-layer-english-hindi-and-bhakti) | [Engineering highlights](#engineering-highlights) | [Local setup](#local-setup) | [Deployment](#deployment) | [Operational notes](#operational-notes) | [Hook engine](#hook-engine) | [Comment replies](#comment-reply-agent) | [Limitations](#known-limitations) | [Roadmap](#roadmap)

---

## Channels in Production

| Channel | Audience / Niche | Content source | Voice | Workflow |
| --- | --- | --- | --- | --- |
| **English** ([@AIcarryONAI](https://www.youtube.com/@AIcarryONAI)) | US, Tech/AI | Trending-topic discovery + LLM research | Edge TTS | `english-scheduler.yml` (every 5h) |
| **Hindi** ([@AICarryONHindi](https://www.youtube.com/@AICarryONHindi)) | India, experiment-style videos (chemical, physical and technology experiments from around the world) | LLM-generated trending topics, Hinglish scripts | Sarvam AI Bulbul, Edge TTS fallback | `hindi-scheduler.yml` (hourly check, adaptive, max 3 uploads/day) |
| **Cricket** ([@AICarryONSports](https://www.youtube.com/@AICarryONCricket)) | Cricket news and match content | CricAPI match data + trending cricket topics | Edge TTS | `cricket-scheduler.yml` |
| **Gaming** ([@AICarryONGaming](https://www.youtube.com/@AICarryONGaming)) | Gaming moments and trends | Twitch Helix Clips API (followed streamers + top clips by game category) | Edge TTS | `gaming-scheduler.yml` |
| **Bhakti** | India, Hindi devotional Shorts (mythological stories, mantra meanings, festival significance) | Devotional topic pool + live trend check, LLM research, Pexels temple/diya/aarti footage, Freesound bhajan-style music | Sarvam AI (devotional pace); `BHAKTI_MODE=music_only` skips voice and captions | `bhakti-scheduler.yml` (hourly check, adaptive, up to 6 uploads/day) |

The Hindi channel was pivoted from tech news to experiment-style videos after poor retention, to make content more visual and interactive.

The Bhakti channel is the fifth pipeline and is documented in [`SETUP_BHAKTI.md`](SETUP_BHAKTI.md). It has two modes: `narrated` (the default: Hindi voiceover and captions over a ducked bhajan track) and `music_only` (bhajan only, no voice). It uses stock footage only, with no AI-generated deity imagery, and it reuses the English/Hindi intelligence layer (saturation, comparison, A/B titles, velocity).

---

## What Each Pipeline Does

Every channel runs on its own schedule and, without human input:

1. Checks whether the current hour matches a learned peak-engagement window (English/Hindi/Bhakti; falls back to safe defaults until enough data exists)
2. Finds a trending topic, filtered to the channel's niche
3. Checks topic saturation and skips topics that are already over-covered (English/Hindi/Bhakti)
4. Benchmarks competitors on the same topic (English/Hindi/Bhakti)
5. Researches the topic and writes a script tuned to the channel's length and style
6. Generates three title variations using different psychological patterns, scores them, and picks a winner
7. Writes the SEO description and hashtags
8. Generates a voiceover and word-by-word captions
9. Pulls background footage: topic-relevant Pexels video clips (English, Hindi, Cricket, Bhakti), or real Twitch clips downloaded with `yt-dlp` (Gaming)
10. Renders the final video with ffmpeg
11. Uploads to YouTube with full metadata
12. Records view snapshots on a schedule for ongoing analytics

### Pipeline (per channel)

```mermaid
flowchart TD
    A["Adaptive hour check"] --> B["Trending topic (niche-filtered)"]
    B --> BR["Brain ranking<br/>(optional, English)"]
    BR --> C["Saturation check"]
    C --> D["Competitor comparison"]
    D --> E["Research (LLM) and script"]
    E --> F["A/B title test and SEO"]
    F --> G["Voiceover and captions"]
    G --> H["Background footage<br/>(Pexels / Twitch clips)"]
    H --> I["Render (ffmpeg)"]
    I --> J["YouTube upload"]
    J --> K["View snapshots to Postgres"]
    K -. "learning loop" .-> A
```

---

## Architecture

**Compute and scheduling.** Each channel is a GitHub Actions cron workflow (`.github/workflows/`). Runs are stateless, so all state lives in Postgres and all credentials in GitHub Secrets.

**LLM routing.** Groq (`openai/gpt-oss-120b`) is the primary model, with Gemini (`gemini-3.5-flash`) as fallback. The English and Hindi routing layers (`model_invoke_agent_english.py`, `model_invoke_agent_hindi.py`) include circuit breakers and per-run call budgets so a provider outage or rate limit degrades gracefully instead of failing a run.

**Data.** Supabase Postgres, accessed through the transaction pooler (port 6543), because GitHub Actions runners are IPv4-only and Supabase direct connections are IPv6-only. English, Hindi and Bhakti share one database, partitioned by a `channel` column so their learning never mixes. Cricket uses its own Supabase project for topic de-duplication.

**Dashboard.** A Streamlit app on Streamlit Community Cloud reads the same Postgres database. Pages: Dashboard, Peak Hours, Schedule, Analytics, Comparison, and A/B Titles. The channel selector covers English, Hindi, and Cricket. The dashboard has no Bhakti tab yet; Bhakti data is stored in the shared Postgres database under `channel='bhakti'`.

**Shared rendering.** Every channel reuses the same video renderer (`agents/video_agent.py`), so a fix or improvement there benefits every channel.

### Repository Layout

| Path | Contents |
| --- | --- |
| `agents/` | English pipeline agents and shared video, voice and caption agents (includes `brain_client.py` and `topic_selection.py`, the Brain platform client) |
| `agents_hindi/` | Hindi (Hinglish) pipeline agents |
| `agents_cricket/` | Cricket pipeline agents |
| `agents_gaming/` | Gaming pipeline agents (Twitch client, clip finder, etc.) |
| `agents_bhakti/` | Bhakti (Hindi devotional) pipeline agents |
| `tests/` | Offline tests (comment engine, Brain topic selection, and more) |
| `pages/` | Streamlit dashboard pages |
| `scheduler*.py` | One scheduler per channel: `scheduler.py`, `scheduler_hindi.py`, `scheduler_cricket.py`, `scheduler_gaming.py`, `scheduler_bhakti.py` |
| `app.py` | Streamlit entrypoint |
| `.github/workflows/` | English, Hindi, Cricket, Gaming and Bhakti scheduler workflows |

---

## Brain Platform backend

AI CarryON uses a separate repository, [**ai-carryon-brain-platform**](https://github.com/Amit-coder78/ai-carryon-brain-platform), as its **decision-making backend**. This repo (the pipeline) is the *client*; the Brain platform is the *service* that ranks candidate topics before a video is made.

**Architecture of the backend:** [open the interactive diagram](https://htmlpreview.github.io/?https://github.com/Amit-coder78/ai-carryon-brain-platform/blob/main/docs/architecture/index.html)

```mermaid
flowchart LR
    subgraph C["ai-carryon (pipeline)"]
        S["scheduler.py"] --> T["topic_selection.py"] --> B["brain_client.py"]
    end
    subgraph P["ai-carryon-brain-platform (backend)"]
        E["Engine (FastAPI)<br/>/v1/runtime/execute<br/>rank + fuse (rrf-v1)"]
        R["Registry API + Postgres<br/>Brains, versions, decisions"]
        E --- R
    end
    B -- "candidates" --> E
    E -- "ranked list" --> B
```

### How the two repos connect

| Piece | Location |
| --- | --- |
| HTTP client for the engine | [`agents/brain_client.py`](agents/brain_client.py) |
| Topic choice using the ranking | [`agents/topic_selection.py`](agents/topic_selection.py), called from `scheduler.py` |
| Engine started inside the CI job | `.github/workflows/english-scheduler.yml` uses `Amit-coder78/ai-carryon-brain-platform/.github/actions/ci-engine@main` |
| API contract both sides follow | [`contracts/openapi.yaml`](https://github.com/Amit-coder78/ai-carryon-brain-platform/blob/main/contracts/openapi.yaml) in the Brain platform repo |

### What the Brain does for the pipeline

1. The scheduler builds a shortlist of trending topics (topics posted in the last 24h are dropped first).
2. It sends them to `POST /v1/runtime/execute` with signals per topic: freshness (`days_since_trend`) and viewer demand from `topic_requests` over 30 days (`unique_requesters` = distinct commenters via `author_channel_id`).
3. The engine runs the channel's attached Brains, fuses their rankings, and returns the order plus a `decision_id`.
4. The pipeline tries topics in Brain order; each must still pass the saturation check.
5. Every Brain-assisted decision logs one `BRAIN_LOG {...}` line (source, execution id, policy version, Brain versions).

### Safe by default

**Off unless configured; with nothing set there is no extra network call and behaviour is unchanged.** Any failure, a `baseline_fallback`, or a ranking with no preference (`no_signal`) falls back to the original selection. A Brain problem can never fail a video run.

### Enabling it (English channel)

Add a GitHub repository **variable** `BRAIN_ENABLED` = `true` (Settings > Secrets and variables > Actions > Variables). The English workflow then starts the engine inside the job, so there is nothing to host. Remove the variable (or set it to anything else) to switch it off. If the engine fails to start, the run continues without the Brain.

To use a separately hosted engine instead, leave `BRAIN_ENABLED` unset and set these yourself:

| Variable | Purpose |
| --- | --- |
| `BRAIN_ENGINE_URL` | Engine base URL, reachable from the runner |
| `BRAIN_WORKSPACE_ID` | Workspace uuid in the Brain registry (sent as `x-workspace-id`) |
| `BRAIN_CHANNEL_ID_ENGLISH` | Registry channel uuid (`BRAIN_CHANNEL_ID_<CHANNEL>` for others) |
| `BRAIN_ENABLED=0` | Kill switch in the environment |

Cost when enabled: one extra YouTube search call per run. Other channels can adopt it with `brain_ranked_topic("<channel>")` and `BRAIN_CHANNEL_ID_<CHANNEL>`.

Tests (offline): `python -m unittest tests.test_brain_topic_selection -v`

---

## Intelligence Layer (English, Hindi and Bhakti)

| Component | What it does |
| --- | --- |
| **Velocity and peak-hour detection** | Computes views gained per hour per video, aggregated by hour of day, to find the best upload windows |
| **Saturation engine** | Scores topics 0-100 from recent competing video count and authority-channel coverage. Fails open if the YouTube API is unavailable |
| **Comparison engine** | Benchmarks the top 10 competing videos (views, engagement, title length, duration) and feeds recommendations into script and SEO generation |
| **A/B title testing** | Generates 3 titles from 8 psychological patterns, scores each on a 10-point rubric, and logs every test for pattern tracking |
| **Adaptive scheduling**              | Scores every UTC hour from an audience-heatmap prior + recent view velocity + 24h views by upload hour (last 28 days), and uploads only in the best 6 hours; the prior can be updated via the DB meta key `audience_prior_ist_<channel>` |
| **View tracking** | Hourly snapshots of views, likes, and comments per video |

---

## Engineering Highlights

- **Moved off paid hosting to a zero-cost deployment.** English and Hindi were migrated from Railway (which began requiring a credit card) to GitHub Actions cron workflows. The SQLite store was replaced with Postgres so state survives across stateless runs. The gaming channel was built directly on the same pattern.
- **Replaced the database layer.** The original per-feature JSON files became a single SQLite database and then Supabase Postgres, with an idempotent migration script and a timezone fix for mixed naive/aware timestamps that had silently broken velocity calculation.
- **Survived a model deprecation.** When Groq retired `llama-3.3-70b-versatile`, roughly 25 hardcoded call sites across all channels were migrated to `openai/gpt-oss-120b`, and a retired Gemini model in the routing layer was replaced.
- **Upgraded visuals from static to dynamic.** Ken-Burns still images were replaced with topic-relevant Pexels video clips for English and Hindi.
- **Fixed silent audio truncation in Hindi TTS.** Sarvam splits long text into multiple WAV segments and only the first was being saved, cutting about 70% of each voiceover. Concatenating all segments fixed it, and script length was retuned to 110-130 words to stay under the 60-second Shorts limit.
- **Built the gaming pipeline end to end.** Twitch OAuth app registration, Helix Clips API integration, per-clip download with `yt-dlp`, and a category filter that automatically excludes non-gameplay categories (Just Chatting, Music, ASMR, etc.) from top-clip discovery.
- **Cricket state moved to Postgres.** De-duplication tracking was moved from a JSON file to Supabase so it survives ephemeral compute.

---

## Local Setup

```bash
git clone https://github.com/Unknown183-a/ai-carryon.git
cd ai-carryon
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Dashboard
streamlit run app.py

# Run a channel scheduler locally
python scheduler.py           # English
python scheduler_hindi.py     # Hindi
python scheduler_cricket.py   # Cricket
python scheduler_gaming.py    # Gaming
python scheduler_bhakti.py    # Bhakti
```

### Configuration

| Variable | Purpose | Scope |
| --- | --- | --- |
| `DATABASE_URL` | Postgres connection (Supabase transaction pooler) | All |
| `GROQ_API_KEY` | Primary LLM inference | All |
| `GEMINI_API_KEY` | LLM fallback | All |
| `PEXELS_API_KEY` | Background clips and images | English, Hindi, Cricket, Bhakti |
| `YOUTUBE_API_KEY` | YouTube Data API (search, public stats) | All |
| `YOUTUBE_TOKEN_B64`, `YOUTUBE_CLIENT_SECRETS_B64`, `YOUTUBE_ANALYTICS_TOKEN_B64` | English OAuth upload and analytics credentials | English |
| `YOUTUBE_TOKEN_JSON` / `HINDI_TOKEN_JSON` | Hindi OAuth token (needs `youtube.upload`, `youtube.readonly` and `youtube.force-ssl` scopes) | Hindi |
| `SARVAM_API_KEY` | Hindi native TTS | Hindi, Bhakti |
| `BHAKTI_TOKEN_JSON` | Bhakti OAuth token (needs `youtube.upload`, `youtube.readonly` and `youtube.force-ssl`) | Bhakti |
| `YOUTUBE_BHAKTI_CHANNEL_ID`, `BHAKTI_AUTHORITY_CHANNEL_IDS` | Own channel id and authority channels for saturation scoring | Bhakti |
| `FREESOUND_API_KEY` | Bhajan-style background music | Bhakti |
| `BHAKTI_MODE`, `BHAKTI_VIDEOS_PER_DAY` | `narrated` (default) or `music_only`; daily upload target (default 6) | Bhakti |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Twitch Helix API | Gaming |
| `YOUTUBE_GAMING_CLIENT_SECRETS_B64`, `YOUTUBE_GAMING_TOKEN_B64` | Gaming channel OAuth credentials | Gaming |
| `APP_PASSWORD` | Streamlit dashboard login | Dashboard |

Cricket additionally needs a CricAPI key and its own YouTube credentials.

---

## Deployment

All five pipelines deploy the same way: push to `main`, and the scheduled workflows in `.github/workflows/` pick up the new code on their next run. Secrets live in GitHub repository secrets. To trigger a run without waiting for the cron, use "Run workflow" on the relevant workflow in the Actions tab.

---

## Operational Notes

- **YouTube API quota**: 10,000 units/day per Google Cloud project, and each upload costs about 1,650 units. The gaming channel uses its own Google Cloud project and OAuth client.
- **API key isolation**: each channel workflow uses its own `GROQ_API_KEY_<CHANNEL>`, `GEMINI_API_KEY_<CHANNEL>` and `PEXELS_API_KEY_<CHANNEL>` secret (Hindi, Cricket and Bhakti also get `SARVAM_API_KEY_<CHANNEL>`). The old shared keys are passed as `*_API_KEY_SHARED`, and `key_fallback.py` retries a call with the shared key only on 429/401/403 (`KEY_FALLBACK=0` disables it). Tests: `python -m unittest tests.test_key_fallback -v`.
- **OAuth tokens** can expire (`invalid_grant`). Re-authenticate locally and update the matching GitHub secret. A Hindi token with upload-only scope silently fails view tracking with a 403.
- **Sarvam text limits**: long scripts come back as multiple audio chunks, so always concatenate every returned segment.
- **Flow/Veo clips**: an optional manual mode for cinematic clips exists in the English and Hindi agents (`flow_prompt_agent.py`), but the automated pipelines use Pexels or Twitch footage. Real brand names in clip prompts can trigger Flow's policy filter.

## Hook Engine

Every Short can open on a topic-relevant visual hook (1-3s), chosen before the normal B-roll.

```
script -> story analysis + hook intent -> 3-5 queries -> Pexels (10-20 candidates)
       -> score (relevance 40 / visual 25 / curiosity 20 / emotion 10 / continuity 5)
       -> relevance >= 70% gate -> duplicate check -> best hook -> render first
```

- Code: `agents/hook_engine.py` (shared core + per-channel profiles), `agents/hook_render.py` (renderer helper), `agents_gaming/gameplay_hook.py`. Pexels search goes through the existing client: `agents/video_clip_agent.py::search_hook_videos()`.
- The hook **replaces** the first seconds of B-roll; total length, voice and caption timing are unchanged.
- English, Hindi, Cricket, Bhakti use Pexels. Bhakti is restricted to reverent hook types and blocks dramatic/violent/deity terms. Gaming cuts a cold open from the real gameplay peak (vision-analysed, intensity >= 6 only).
- Relevance is judged from the Pexels clip **title** (no tags/description exist), blended 75/25 with a keyword-overlap check. No suitable hook -> the video renders exactly as before.
- Duplicates: per-channel history in the DB meta table (`hook_history:<channel>`), 14-day cooldown, recorded only after a successful upload. Selections are logged as `HOOK_LOG {...}` lines.
- Env: `HOOK_ENGINE=0` (off everywhere), `HOOK_ENGINE_<CHANNEL>=0`, `HOOK_MIN_RELEVANCE`, `HOOK_REUSE_COOLDOWN_DAYS`, `GAMING_HOOK_MIN_INTENSITY`.
- Tests: `python -m unittest tests.test_hook_engine -v`

## Comment Reply Agent

One shared engine (`agents/comment_engine.py`) with thin per-channel wrappers
(`comment_reply_agent.py` in `agents/`, `agents_hindi/`, `agents_bhakti/`, `agents_cricket/`). English, Hindi and Bhakti run it from
their scheduler once per IST day (any time 19:00-23:59 IST, tracked in the DB so a late GitHub cron still
counts); Cricket runs it from its own `14:00 UTC` cron (`--comments`).

- One LLM call per comment returns category + reply + topic. Spam/Offensive are never answered.
- Bhakti has a devotional voice: it mirrors greetings ("Jai Shree Ram" gets the same greeting back), never jokes about deities, never promises miracles, and never answers religious arguments (`Religious Debate` is skipped like Spam).
- State lives in the database (`comment_history`, `topic_requests`; cricket: `cricket_comment_history`,
  `cricket_match_requests`), not `output/*.json`, which Actions wipes every run.
- A comment is only finished once the reply is posted, deliberately skipped, or failed permanently. LLM/API
  hiccups are retried on the next run (3 attempts).
- A missing `youtube.force-ssl` scope, expired token or exhausted quota aborts the run with the fix in the log.
- Skips the channel's own comments and threads that already have a reply; never posts links or @mentions.
- Per-run caps: 25 replies, 15 minutes, newest 250 threads.
- `COMMENT_AUTO_REPLY=false` is a dry run (drafts stored, nothing posted); `COMMENT_FORCE=true` ignores the
  window. Both are inputs on the English/Hindi/Bhakti/Cricket workflows' manual "Run workflow" button.
- Tokens must include `youtube.force-ssl`: `generate_english_token.py`, `generate_hindi_token_v3.py`,
  `generate_bhakti_token.py`, `generate_cricket_token.py` (the old cricket and bhakti tokens cannot post; regenerate them).
- Tests: `python -m unittest tests.test_comment_engine -v`

## Known Limitations

- Sarvam Hindi TTS currently errors on `target_language_code` and falls back to Edge TTS
- Custom thumbnail upload is blocked by a 403 on some channels (likely requires channel phone verification)
- The comment-reply agent needs the `youtube.force-ssl` OAuth scope (see the Comment Reply Agent section); Gaming has no comment agent yet
- Bhakti has no tab in the Streamlit dashboard yet (its data is in the database)
- Saturation gating is not bypassed by `FORCE_GENERATE` (only the schedule-hour gate is)

---

## Roadmap

- **Close the A/B loop**: title scores are LLM-predicted today. Validate them against real 24h view counts and let pattern selection use ground truth.
- **Extend the intelligence layer to Cricket and Gaming**: saturation, comparison, and adaptive scheduling currently run for English and Hindi only.
- **Failure and hook analysis**: systematic tracking of why videos underperform.
- **Audience, opportunity, and monetization intelligence**: demographic pulls, pre-trend prediction, and RPM-correlated topic scoring.
- **Centralized "brain" service**: built as [ai-carryon-brain-platform](https://github.com/Amit-coder78/ai-carryon-brain-platform) and live for English topic ranking (see [Brain Platform backend](#brain-platform-backend)). Next: roll it out to the other channels.
- **Multi-tenant SaaS rebuild**: a from-scratch rebuild of this pipeline as a multi-tenant platform (FastAPI, Firebase, Celery workers on Cloud Run) lives in [ai-carryon-saas](https://github.com/Unknown183-a/ai-carryon-saas).

---

## Additional pipelines

| Pipeline | Description | Docs |
| --- | --- | --- |
| **Finance** | Stock-market and finance education Shorts with a fact-check gate on every script | [`FINANCE.md`](FINANCE.md) |
| **Horror** | Original English horror stories (clearly labelled fiction), Shorts or long-form | [`HORROR.md`](HORROR.md) |

---

<div align="center">

**AI CarryON** | Built by Amit Kumar

</div>
