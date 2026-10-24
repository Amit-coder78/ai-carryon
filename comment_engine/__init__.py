"""AI CarryON Comment Agent: one shared engine, seven channel personas.

See README.md ("Comment Agent") and AI_CarryON_Comment_Agent_Implementation.md.
Publishing is never implicit: drafts -> owner approval -> explicit publish.
"""
from .config import PIPELINES, PIPELINE_PERSONA, ConfigError, Settings, load_settings  # noqa: F401
