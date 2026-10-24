"""Per-channel wiring to the repo's existing credentials and LLM wrappers (lazy imports).

Each channel keeps its OWN token; nothing is shared across channels. Unattended runs
must never fall into an interactive OAuth flow, so we require a token to be present first.
"""
import importlib
import os

from .config import PIPELINES
from .llmjson import text_of

# channel -> (env vars that may hold the token, local fallback file, regeneration hint)
TOKENS = {
    "english": (("YOUTUBE_TOKEN_B64",), "token.pickle", "generate_english_token.py"),
    "hindi": (("YOUTUBE_TOKEN_JSON", "HINDI_TOKEN_JSON"), None, "generate_hindi_token_v3.py"),
    "bhakti": (("BHAKTI_TOKEN_JSON",), None, "generate_bhakti_token.py"),
    "cricket": (("CRICKET_YOUTUBE_TOKEN_B64",), "cricket_token.pickle", "generate_cricket_token.py"),
    "gaming": (("YOUTUBE_GAMING_TOKEN_B64", "GAMING_YOUTUBE_TOKEN_B64"), "gaming_token.pickle",
               "generate_gaming_token.py"),
    "finance": (("YOUTUBE_FINANCE_TOKEN_B64", "FINANCE_TOKEN_JSON"), None, "generate_finance_token.py"),
    "horror": (("YOUTUBE_HORROR_TOKEN_B64", "HORROR_YOUTUBE_TOKEN_B64"), "horror_token.pickle",
               "generate_horror_token.py"),
}

# channel -> (module, function) returning an authenticated googleapiclient resource
CLIENTS = {
    "english": ("agents.upload_agent", "authenticate_youtube_headless"),
    "hindi": ("agents_hindi.upload_agent", "get_youtube_client_readonly"),
    "bhakti": ("agents_bhakti.upload_agent", "get_youtube_client_readonly"),
    "cricket": ("agents_cricket.upload_agent", "authenticate_youtube"),
    "gaming": ("agents_gaming.upload_agent", "authenticate_youtube"),
    "finance": ("agents_finance.upload_agent", "get_youtube_client"),
    "horror": ("agents_horror.upload_agent", "authenticate_youtube"),
}

# channel -> (module, function, returns_message_object)
LLMS = {
    "english": ("agents.model_invoke_agent_english", "safe_invoke"),
    "hindi": ("agents_hindi.model_invoke_agent_hindi", "safe_invoke"),
    "bhakti": ("agents_bhakti.model_invoke_agent_bhakti", "safe_invoke"),
    "cricket": ("agents_hindi.model_invoke_agent_hindi", "safe_invoke"),
    "gaming": ("agents.model_invoke_agent_english", "safe_invoke"),
    "finance": ("agents_finance.llm", "invoke"),
    "horror": ("agents_cricket.script_agent", "safe_invoke"),
}


class MissingCredentials(RuntimeError):
    pass


def has_token(channel_id):
    envs, fallback, _ = TOKENS[channel_id]
    return any(os.getenv(e) for e in envs) or bool(fallback and os.path.exists(fallback))


def build_youtube(channel_id):
    if channel_id not in PIPELINES:
        raise ValueError(f"unknown channel {channel_id!r}")
    if not has_token(channel_id):
        envs, _, hint = TOKENS[channel_id]
        raise MissingCredentials(f"no YouTube token for {channel_id}: set one of {envs} or run {hint}")
    mod, fn = CLIENTS[channel_id]
    return getattr(importlib.import_module(mod), fn)()


def build_llm(channel_id):
    """Return callable(prompt) -> str using the channel's existing model wrapper."""
    mod, fn = LLMS[channel_id]
    raw = getattr(importlib.import_module(mod), fn)

    def invoke(prompt):
        return str(text_of(raw(prompt)))
    invoke.model_version = f"{mod}.{fn}"
    return invoke


def save_topic_request(channel_id, topic, comment_text, video_id, author_channel_id=None):
    """Default topic sink: viewer video requests -> the tables other parts of the repo read
    (topic_requests for the Brain demand signal; cricket_match_requests for cricket)."""
    if channel_id == "cricket":
        from agents_cricket.database import db as cricket_db
        cricket_db.save_match_request(request_text=topic, comment=comment_text, video_id=video_id)
    else:
        from agents.database import db
        db.save_topic_request(channel_id, topic, comment_text, video_id, author_channel_id=author_channel_id)
