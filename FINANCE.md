# AI CarryON Finance

Sixth pipeline, added beside the existing five. English, stock-market and finance education, Shorts (45-60s, 9:16) by default, long-form optional.

## How it fits the existing system (inspected, not assumed)
- Pipelines here are **independent scheduler scripts + GitHub Actions cron workflows + a channel tag in the shared Postgres**; there is no central registry, FastAPI app or LangGraph. Finance is registered the same way: `scheduler_finance.py` + `.github/workflows/finance-scheduler.yml`.
- Reused as-is: `agents.run_state.RunState` (resume), the voice/caption/render calls Cricket uses, Pexels footage via `agents.video_clip_agent.download_video_clip`, `DATABASE_URL` Postgres (own `finance_meta` / `finance_posted` tables).
- New and Finance-only (`agents_finance/`): config, prompts, deterministic calculations, fact registry, fact-checker, topics, research, market data, scene planner, charts, metadata.

## Pipeline
topic (grounded) -> research -> market data -> deterministic facts -> script (LLM writes `{{fact_id}}` placeholders only) -> **fact-check gate (retry loop)** -> scenes -> voice -> captions -> media (Pexels generic footage + charts from verified series) -> render -> review bundle (default) or upload.

Key safety design: the model never types a number. Data enters via `{{id}}`, `{{id.dir}}`, `{{id.abs}}` and is substituted from verified facts. Any digit the model types is rejected (`UNGROUNDED_NUMBER`). Blocking checks: stale data, unsourced/unattributed facts, unlabeled hypotheticals, guarantee/buy-sell language, missing risk statement (derivatives, options, IPO, technical, investing, funds, company analysis), contradictions, no fresh data for news/analytical content.

## Environment variables
Existing, reused: `DATABASE_URL`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `PEXELS_API_KEY`.
New (all optional): `FINANCE_CATEGORY` (indian_markets, global_markets, company_analysis, ipo, technical_analysis, derivatives, investing, mutual_funds_etfs, macro, news, sectors, history, psychology_personal_finance), `FINANCE_CONTENT_TYPE` (educational|analytical|historical|news), `FINANCE_MARKET` (india|global), `FINANCE_TICKER`, `FINANCE_FORMAT` (shorts|long), `FINANCE_DURATION_SEC` (shorts 45-60, long 120-1200), `FINANCE_LANGUAGE` (en), `FINANCE_SUBTITLES`, `FINANCE_MUSIC`, `FINANCE_REQUIRE_REVIEW` (**default true**), `FINANCE_MAX_RETRIES`, `FINANCE_DAILY_UPLOAD_CAP`, `FINANCE_DATA_MAX_AGE_HOURS` (72), `FINANCE_RESEARCH_MAX_AGE_HOURS` (48), `FINANCE_FEEDS` (JSON `[{"url","publisher","tier"?}]`), `FINANCE_VOICE_IMPL` / `FINANCE_CAPTIONS_IMPL` / `FINANCE_VIDEO_IMPL` (`module:function` overrides).

## Data providers (actual)
- Prices: **Stooq daily CSV**, free, no key, end-of-day only (not live), coverage depends on Stooq symbols, non-commercial terms. Check the Indian symbols you need before relying on them. Swappable: any object with `.daily_bars(symbol, days)`.
- News/announcements: **none bundled.** Add official RSS feeds (exchange, RBI, SEBI, central banks) in `FINANCE_FEEDS` after verifying each URL. Unrated domains are dropped; with no feeds, news topics are skipped, never invented.
- FII/DII, IPO subscription, fund NAV/expense ratios, fundamentals (P/E, margins) have **no provider yet**; those topics run as evergreen explainers until one is added (calculations for them already exist and are tested).

## Limitations / not done
- **Auto-upload** is implemented in `agents_finance/upload_agent.py` (secret `FINANCE_TOKEN_JSON`, uploads `private` unless `FINANCE_PRIVACY` says otherwise) but off by default. Default is review mode: the run saves `output/finance_review/<topic>/` (video, script, facts, scenes, metadata with sources) and the workflow uploads it as an artifact.
- **Dashboard tab not added** (`pages/` could not be read).
- No hook engine for Finance (its profiles are per channel). Background music option is accepted but not wired (renderer's music interface not inspected).
- Number grounding covers digits; spelled-out figures ("two lakh crore") are not machine-checked. Direction/semantics of interpretation are LLM-written and need review.
- Regulatory: SEBI rules for finance content/advice (registration, disclosures) must be verified by you or counsel; the built-in disclaimer does not replace that.

## Verify before first real run (could not be confirmed from GitHub's rendered pages)
1. `agents_cricket.voice_agent.generate_voice` and `agents_cricket.video_agent.create_video` are used because their call shape is visible in `scheduler_cricket.py`; confirm they have no Cricket-specific voice/branding, or point `FINANCE_VOICE_IMPL` / `FINANCE_VIDEO_IMPL` at the English shared versions.
2. The renderer reads clips from `assets/pexels_clips/N.mp4` (the folder the shared downloader writes to). Confirm `create_video` picks them up when `use_pexels_clips=True`.
3. `agents.video_clip_agent.download_video_clip(prompt, output_path)` exists in `agents/` (seen in the root copy).
