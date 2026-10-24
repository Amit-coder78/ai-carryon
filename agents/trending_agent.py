# agents/trending_agent.py
import os
import re
import json
import random
import googleapiclient.discovery
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

RECENT_TOPICS_FILE = "output/recent_trending_topics.json"
RECENT_DAYS = 7  # don't repeat a topic used within this many days

# ONLY Science & Technology category
CATEGORY_IDS = ["28"]

# Must contain at least one of these to qualify
TECH_MUST_HAVE = [
    "ai", "artificial intelligence", "robot", "iphone", "samsung", "apple",
    "google", "microsoft", "tech", "chip", "cpu", "gpu", "phone", "laptop",
    "app", "software", "hack", "cyber", "data", "camera", "battery", "5g",
    "electric", "gadget", "device", "computer", "internet", "crypto",
    "space", "nasa", "tesla", "elon", "openai", "chatgpt", "gemini",
    "meta", "vr", "ar", "drone", "satellite", "quantum", "neural", "model",
    "invention", "technology", "innovation", "future", "new feature",
    "launched", "released", "revealed", "upgrade", "update", "version",
    "humanoid", "autonomous", "self-driving", "electric vehicle", "ev",
    "gpt", "llm", "machine learning", "deep learning", "automation",
]

# Immediately reject if contains any of these
TECH_BLOCKLIST = [
    "gift", "wedding", "love", "relationship", "dating", "family",
    "cooking", "recipe", "food", "fashion", "makeup", "beauty",
    "sport", "football", "cricket", "basketball", "soccer", "tennis",
    "music", "song", "album", "concert", "dance", "movie", "film",
    "drama", "series", "netflix", "disney", "celebrity", "actor",
    "politics", "election", "trump", "biden", "government", "war",
    "iran", "russia", "ukraine", "military", "president", "senator",
    "religion", "god", "prayer", "church", "mosque", "temple",
    "fitness", "workout", "diet", "weight loss", "gym", "yoga",
    "travel", "hotel", "vacation", "tourism", "beach", "holiday",
    "real estate", "house", "home decor", "interior", "garden",
    "animals", "pet", "dog", "cat", "wildlife", "nature",
    "funny", "prank", "challenge", "reaction", "vlog", "storytime",
    "she", "he", "her", "him", "waited", "gave", "surprised",
]


def is_tech_topic(title: str) -> bool:
    title_lower = title.lower()
    if any(kw in title_lower for kw in TECH_BLOCKLIST):
        return False
    if any(kw in title_lower for kw in TECH_MUST_HAVE):
        return True
    return False


# ─────────────────────────────────────────────
# Recent-topic memory (prevents repeats)
# ─────────────────────────────────────────────

def _normalize(title: str) -> str:
    """Lowercase, strip punctuation/emoji/hashtags, collapse whitespace."""
    t = title.lower()
    t = re.sub(r"#\w+", "", t)              # remove hashtags
    t = re.sub(r"[^\w\s]", " ", t)          # remove punctuation/emoji
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _load_recent():
    if os.path.exists(RECENT_TOPICS_FILE):
        try:
            with open(RECENT_TOPICS_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []
    else:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    fresh = []
    for entry in data:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            if ts >= cutoff:
                fresh.append(entry)
        except Exception:
            continue
    return fresh


def _save_recent(title: str):
    os.makedirs("output", exist_ok=True)
    recent = _load_recent()
    recent.append({
        "title": title,
        "normalized": _normalize(title),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    with open(RECENT_TOPICS_FILE, "w") as f:
        json.dump(recent, f, indent=2, ensure_ascii=False)


def _is_repeat(title: str, recent_entries) -> bool:
    norm = _normalize(title)
    for entry in recent_entries:
        if entry["normalized"] == norm:
            return True
        # near-duplicate check: same first 5 words
        a = norm.split()[:3]
        b = entry["normalized"].split()[:3]
        if a and a == b:
            return True
    return False


def _load_uploaded_titles():
    """
    Permanently exclude topics that were already turned into a real
    uploaded video, regardless of how long ago. Reads directly from
    output/aicarryon.db (videos table).
    """
    from agents.database import db
    uploaded = []
    try:
        videos = db.get_all_videos()
        for v in videos:
            if v.get("channel") not in ("english", None):
                continue
            title = v.get("title")
            if title:
                uploaded.append({"title": title, "normalized": _normalize(title)})
    except Exception as e:
        print(f"Trending agent: could not load uploaded titles from DB: {e}")
    return uploaded


# ─────────────────────────────────────────────
# Fetch: recent search (primary) + mostPopular chart (fallback)
# ─────────────────────────────────────────────

def _fetch_recent_search_items(youtube, region_code, hours=96, max_results=50):
    """Genuinely fresh videos from the last N hours, sorted by view count, as (title, publishedAt)."""
    published_after = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        search_resp = youtube.search().list(
            part="snippet",
            type="video",
            order="viewCount",
            publishedAfter=published_after,
            regionCode=region_code,
            videoCategoryId="28",
            maxResults=max_results,
        ).execute()
        return [(item["snippet"]["title"], item["snippet"].get("publishedAt"))
                for item in search_resp.get("items", [])]
    except Exception as e:
        print(f"Trending agent recent-search error: {e}")
        return []


def _fetch_recent_search(youtube, region_code, hours=96, max_results=50):
    """Genuinely fresh videos from the last N hours, sorted by view count."""
    return [t for t, _ in _fetch_recent_search_items(youtube, region_code, hours, max_results)]


def _fetch_most_popular(youtube, region_code, max_results=50):
    try:
        response = youtube.videos().list(
            part="snippet,statistics",
            chart="mostPopular",
            regionCode=region_code,
            maxResults=max_results,
            videoCategoryId="28",
        ).execute()
        return response.get("items", [])
    except Exception as e:
        print(f"Trending agent mostPopular error: {e}")
        return []


def get_trending_topic(region_code="US"):
    try:
        youtube = googleapiclient.discovery.build(
            "youtube", "v3", developerKey=YOUTUBE_API_KEY
        )

        recent_used = _load_recent() + _load_uploaded_titles()

        # Primary: genuinely fresh videos from last 48h
        fresh_titles = _fetch_recent_search(youtube, region_code, hours=96)
        candidates = [t for t in fresh_titles if all(ord(c) < 128 for c in t) and is_tech_topic(t)]
        candidates = [t for t in candidates if not _is_repeat(t, recent_used)]

        if candidates:
            chosen = random.choice(candidates[:20])
            _save_recent(chosen)
            return chosen

        # Fallback: mostPopular chart, scored by views (old behavior)
        all_videos = _fetch_most_popular(youtube, region_code)
        scored = []
        for video in all_videos:
            snippet = video["snippet"]
            stats = video.get("statistics", {})
            title = snippet["title"]
            views = int(stats.get("viewCount", 0))

            if not all(ord(c) < 128 for c in title):
                continue
            if not is_tech_topic(title):
                continue
            if _is_repeat(title, recent_used):
                continue

            score = views
            try:
                published = datetime.fromisoformat(
                    snippet["publishedAt"].replace("Z", "+00:00")
                )
                if published >= datetime.now(timezone.utc) - timedelta(hours=48):
                    score *= 1.5
            except Exception:
                pass

            scored.append((score, title))

        if scored:
            scored.sort(reverse=True)
            top_pool = [title for _, title in scored[:10]]
            chosen = random.choice(top_pool)
            _save_recent(chosen)
            return chosen

    except Exception as e:
        print(f"Trending agent error: {e}")

    chosen = _fallback_topic()
    _save_recent(chosen)
    return chosen


def _age_days(published_iso):
    try:
        published = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - published).total_seconds() / 86400.0, 0.0)
    except Exception:
        return None


def get_trending_candidates(region_code="US", limit=20):
    """The same filtered shortlist get_trending_topic() draws its random pick from, as
    [{"title", "age_days"}], with NO side effects (nothing is marked as used). Returns []
    on any failure; callers then use get_trending_topic() as before."""
    try:
        youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        recent_used = _load_recent() + _load_uploaded_titles()
        items = _fetch_recent_search_items(youtube, region_code, hours=96)
        out = []
        for title, published in items:
            if all(ord(c) < 128 for c in title) and is_tech_topic(title) and not _is_repeat(title, recent_used):
                out.append({"title": title, "age_days": _age_days(published) if published else None})
        return out[:limit]
    except Exception as e:
        print(f"Trending agent candidates error: {e}")
        return []


def mark_topic_used(title):
    """Record a chosen topic the way get_trending_topic() does, so it isn't picked again."""
    _save_recent(title)


def _generate_dynamic_topic(recent_used, attempts=3):
    """
    Ask the LLM to invent a fresh tech video topic when the static
    fallback list has been exhausted. Retries a few times if the LLM
    happens to produce something too close to a recently used topic.
    Routed through model_invoke_agent_english — shares the run's Groq
    budget/circuit breaker and falls back to Gemini automatically.
    """
    from agents.model_invoke_agent_english import safe_invoke

    used_titles = [e["title"] for e in recent_used][-25:]  # keep prompt short
    used_list = "\n".join(f"- {t}" for t in used_titles) if used_titles else "(none yet)"

    prompt = f"""Generate ONE punchy YouTube video title idea about a technology or AI topic
(new gadgets, AI tools, robots, phones, space tech, chips, EVs, etc). It should sound
like a viral tech-news short, similar in style to these already-used titles — but
NOT similar in content or wording to ANY of them:

{used_list}

Rules:
- Under 12 words
- No quotes, no hashtags, no emojis
- Reply with ONLY the title text, nothing else"""

    for _ in range(attempts):
        try:
            response = safe_invoke(prompt, temperature=0.9)
            title = response.content.strip().strip('"')
            if title and not _is_repeat(title, recent_used):
                return title
        except Exception as e:
            print(f"Trending agent: dynamic fallback generation failed: {e}")
            break

    return None


def _fallback_topic():
    """Proven high-performing tech topics, excluding recently used ones.
    Falls through to LLM-generated topics if the static list is exhausted."""
    fallbacks = [
        "New AI robot that can do everything humans can",
        "Apple just revealed iPhone 17 Pro hidden features",
        "Google's new AI beats every model in the world",
        "This new invention will change how we use phones forever",
        "Tesla's new self-driving update just changed everything",
        "New humanoid robot just launched and it's insane",
        "OpenAI just released something nobody expected",
        "Samsung Galaxy S26 leaked features are mind blowing",
        "This AI tool replaces 10 apps for free",
        "New chip technology makes phones 10x faster",
        "NASA just discovered something that changes space forever",
        "The AI that can clone your voice in 3 seconds",
        "New electric vehicle beats Tesla at half the price",
        "This free AI tool is replacing expensive software",
        "Google just launched the most powerful AI assistant ever",
        "This new chip is faster than anything on the market",
        "Apple's secret AI project just leaked online",
        "This robot can now cook a full meal by itself",
        "New drone technology can fly for 10 hours straight",
        "This smart home gadget sold out in one day",
        "Scientists just built a computer that thinks like a brain",
        "This AI can write full apps in seconds",
        "New satellite internet is faster than fiber",
        "This gadget turns your phone into a laptop",
        "New battery tech charges your phone in 5 minutes",
        "This AI model just passed a real medical exam",
        "New VR headset feels indistinguishable from reality",
        "This startup just built a flying car that actually works",
        "New chip inside your phone can run AI offline",
        "This robot dog can now open doors and climb stairs",
    ]
    recent_used = _load_recent() + _load_uploaded_titles()
    unused = [t for t in fallbacks if not _is_repeat(t, recent_used)]

    if unused:
        return random.choice(unused)

    # Static list exhausted — generate a fresh one dynamically
    dynamic = _generate_dynamic_topic(recent_used)
    if dynamic:
        return dynamic

    # Absolute last resort: reuse an old one rather than crash
    return random.choice(fallbacks)
