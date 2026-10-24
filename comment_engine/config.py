"""Settings, pipeline registry and prompt versions. Misconfiguration fails closed."""
import os
from dataclasses import dataclass

# Canonical internal channel ids (the same keys every scheduler/DB already uses).
PIPELINES = ("cricket", "gaming", "finance", "horror", "english", "hindi", "bhakti")

# pipeline -> persona file (comment_personas/<persona_id>.yaml)
PIPELINE_PERSONA = {
    "cricket": "cricket",
    "gaming": "gaming",
    "finance": "finance",
    "horror": "horror",
    "english": "english_tech",
    "hindi": "hindi_tech",
    "bhakti": "bhakti",
}

PROMPT_VERSIONS = {
    "classifier": "comment-classifier-v1",
    "generator": "comment-generator-v1",
    "validator": "comment-validator-v1",
}

MAX_ATTEMPTS = 3                 # retryable failures per comment before dead-letter (blocked)
CLASSIFIER_MIN_CONFIDENCE = 0.35  # one weak signal only; the model's confidence is not calibrated
STALE_PROCESSING_MINUTES = 30
STALE_PUBLISHING_MINUTES = 10


class ConfigError(ValueError):
    pass


def _bool(env, name, default):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _int(env, name, default, lo=0):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if v < lo:
        raise ConfigError(f"{name} must be >= {lo}, got {v}")
    return v


@dataclass(frozen=True)
class Settings:
    enabled: bool = False                 # global kill switch (default off)
    default_mode: str = "draft"           # draft | publish_approved
    channels: tuple = ("cricket",)        # channels the scheduler may run (rollout list)
    max_comments_per_run: int = 20
    max_regeneration_attempts: int = 1
    duplicate_window: int = 100
    require_approval: bool = True
    semantic_check: bool = True
    tenant_id: str = "default"
    window_start_hour_ist: int = 19
    window_end_hour_ist: int = 23
    run_budget_seconds: int = 900
    publish_delay_seconds: float = 2.0
    max_reply_chars: int = 500


def load_settings(env=None) -> Settings:
    e = os.environ if env is None else env
    mode = (e.get("COMMENT_AGENT_DEFAULT_MODE") or "draft").strip().lower()
    if mode not in ("draft", "publish_approved"):
        raise ConfigError(f"COMMENT_AGENT_DEFAULT_MODE must be draft|publish_approved, got {mode!r}")
    chans = tuple(c.strip().lower() for c in (e.get("COMMENT_AGENT_CHANNELS") or "cricket").split(",") if c.strip())
    bad = [c for c in chans if c not in PIPELINES]
    if bad:
        raise ConfigError(f"COMMENT_AGENT_CHANNELS has unknown channel(s): {bad}")
    return Settings(
        enabled=_bool(e, "COMMENT_AGENT_ENABLED", False),
        default_mode=mode,
        channels=chans,
        max_comments_per_run=_int(e, "COMMENT_AGENT_MAX_COMMENTS_PER_RUN", 20, 1),
        max_regeneration_attempts=_int(e, "COMMENT_AGENT_MAX_REGENERATION_ATTEMPTS", 1, 0),
        duplicate_window=_int(e, "COMMENT_AGENT_DUPLICATE_WINDOW", 100, 1),
        require_approval=_bool(e, "COMMENT_AGENT_REQUIRE_APPROVAL", True),
        semantic_check=_bool(e, "COMMENT_AGENT_SEMANTIC_CHECK", True),
        tenant_id=(e.get("COMMENT_AGENT_TENANT_ID") or "default").strip() or "default",
        window_start_hour_ist=_int(e, "COMMENT_WINDOW_START_HOUR_IST", 19, 0),
        window_end_hour_ist=_int(e, "COMMENT_WINDOW_END_HOUR_IST", 23, 0),
        run_budget_seconds=_int(e, "COMMENT_RUN_BUDGET_SECONDS", 900, 1),
        publish_delay_seconds=float(e.get("COMMENT_PUBLISH_DELAY_SECONDS") or 2),
        max_reply_chars=_int(e, "COMMENT_AGENT_MAX_REPLY_CHARS", 500, 20),
    )
