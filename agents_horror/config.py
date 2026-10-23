# agents_horror/config.py
"""Horror-only settings. Every env var is HORROR_*-prefixed so nothing here can
change another pipeline. Unset or empty -> the defaults below."""
import os
import random
from dataclasses import dataclass, field, asdict

CHANNEL = "horror"
CHANNEL_NAME = "AI CarryON Horror"
FICTION_LABEL = "A work of fiction. Not a real incident."

CATEGORIES = {
    "supernatural": "Haunted houses, ghosts, cursed objects, strange night events",
    "psychological": "Unreliable narrator, paranoia, isolation, unexplained sounds and shadows",
    "folklore": "Folklore-inspired fiction: hostels, villages, mansions, railway stations, rural legends",
    "mystery": "Disappearances, locked rooms, strange phone calls, unexpected visitors, twists",
    "creature": "Original fictional entities and monsters with consistent descriptions",
}
FORMATS = ("shorts", "long")
MODES = ("standalone", "episodic")

SHORTS_MIN_SEC, SHORTS_MAX_SEC = 45, 60
LONG_MIN_SEC, LONG_MAX_SEC = 90, 1200
WORDS_PER_SECOND = 2.3  # suspenseful pace (slower than the 2.7 used by the Hindi/Sarvam voice)


def _env(name, default=""):
    v = os.environ.get(f"HORROR_{name}")
    return default if v is None or v == "" else v


def _env_int(name, default):
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(_env(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    v = str(_env(name, "1" if default else "0")).strip().lower()
    return v not in ("0", "false", "no", "off", "")


@dataclass
class HorrorConfig:
    channel_name: str = CHANNEL_NAME
    language: str = "en"
    category: str = ""            # "" -> rotate (least recently used)
    format: str = "shorts"        # shorts | long
    duration_sec: int = 0         # 0 -> default for the format
    audience: str = "horror, supernatural fiction, mystery and suspense fans"
    intensity: int = 3            # 1 (quiet dread) .. 5 (very tense, still no gore)
    setting: str = ""             # "" -> the model picks one
    mode: str = "standalone"      # standalone | episodic
    series_id: str = "default"
    idea: str = ""                # user-provided premise (optional)
    subtitles: bool = True
    music: bool = True            # ambience bed + sfx (Freesound CC0/CC-BY only)
    research: bool = False        # optional factual lookup for folklore/real places
    max_llm_calls: int = 8        # hard cap per run (free-tier quota protection)
    max_scene_retries: int = 2    # per-scene clip re-search, never a full re-render
    min_relevance: float = 0.34   # a clip must match >= this share of the query words
    max_sfx: int = 4
    upload: bool = True           # False -> render only (dry run)
    daily_cap: int = 2
    min_gap_hours: float = 4.0
    extras: dict = field(default_factory=dict)

    # ── helpers ──────────────────────────────────────────────────────
    @property
    def is_long(self):
        return self.format == "long"

    @property
    def target_words(self):
        return int(self.duration_sec * WORDS_PER_SECOND * 0.92)  # ~8% of the time is pauses

    def to_dict(self):
        return asdict(self)

    def validate(self):
        """Clamp/normalise in place and return a list of human-readable notes."""
        notes = []
        self.language = "en"  # only English is supported by this pipeline
        if self.category and self.category not in CATEGORIES:
            notes.append(f"unknown category '{self.category}' -> rotating")
            self.category = ""
        if self.format not in FORMATS:
            notes.append(f"unknown format '{self.format}' -> shorts")
            self.format = "shorts"
        if self.mode not in MODES:
            notes.append(f"unknown mode '{self.mode}' -> standalone")
            self.mode = "standalone"
        lo, hi = (LONG_MIN_SEC, LONG_MAX_SEC) if self.is_long else (SHORTS_MIN_SEC, SHORTS_MAX_SEC)
        if not self.duration_sec:
            self.duration_sec = 300 if self.is_long else 52
        clamped = max(lo, min(hi, int(self.duration_sec)))
        if clamped != self.duration_sec:
            notes.append(f"duration {self.duration_sec}s clamped to {clamped}s for {self.format}")
        self.duration_sec = clamped
        self.intensity = max(1, min(5, int(self.intensity)))
        self.max_llm_calls = max(4, int(self.max_llm_calls))
        self.max_sfx = max(0, int(self.max_sfx))
        self.series_id = "".join(c for c in (self.series_id or "default").lower()
                                 if c.isalnum() or c in "-_")[:40] or "default"
        return notes

    @classmethod
    def from_env(cls, **overrides):
        cfg = cls(
            category=_env("CATEGORY", "").lower(),
            format=_env("FORMAT", "shorts").lower(),
            duration_sec=_env_int("DURATION", 0),
            intensity=_env_int("INTENSITY", 3),
            setting=_env("SETTING", ""),
            mode=_env("MODE", "standalone").lower(),
            series_id=_env("SERIES_ID", "default"),
            idea=_env("IDEA", ""),
            subtitles=_env_bool("SUBTITLES", True),
            music=_env_bool("MUSIC", True),
            research=_env_bool("RESEARCH", False),
            max_llm_calls=_env_int("MAX_LLM_CALLS", 8),
            max_scene_retries=_env_int("MAX_SCENE_RETRIES", 2),
            min_relevance=_env_float("MIN_RELEVANCE", 0.34),
            max_sfx=_env_int("MAX_SFX", 4),
            upload=_env_bool("UPLOAD", True),
            daily_cap=_env_int("DAILY_UPLOAD_CAP", 2),
            min_gap_hours=_env_float("MIN_GAP_HOURS", 4.0),
        )
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        cfg.validate()
        return cfg


def pick_category(recent, rng=random):
    """Least-recently-used category, so the channel does not repeat one theme."""
    recent = [c for c in (recent or []) if c in CATEGORIES]
    unused = [c for c in CATEGORIES if c not in recent]
    if unused:
        return rng.choice(unused)
    last_seen = {c: max(i for i, r in enumerate(recent) if r == c) for c in CATEGORIES}
    return min(CATEGORIES, key=lambda c: last_seen[c])
