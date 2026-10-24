"""Versioned persona loading. One persona per pipeline; errors fail closed."""
import copy
import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .config import PIPELINES, PIPELINE_PERSONA
from .schemas import PersonaConfig

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "comment_personas"

# Owner-editable without code changes (spec section 2). Nothing else is overridable.
ALLOWED_OVERRIDES = {
    "language",
    "voice.humor_level", "voice.assertiveness", "voice.emoji_level",
    "voice.slang_level", "voice.max_sentences",
    "publishing.mode", "publishing.max_replies_per_run",
}


class PersonaError(Exception):
    pass


class PersonaRegistry:
    def __init__(self, directory=None, repository=None):
        self.directory = Path(directory) if directory else DEFAULT_DIR
        self.repository = repository
        self._defaults = {}

    def _read(self, persona_id):
        if persona_id in self._defaults:
            return self._defaults[persona_id]
        path = self.directory / f"{persona_id}.yaml"
        if not path.is_file():
            raise PersonaError(f"persona file missing: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            persona = PersonaConfig.model_validate(data)
        except (yaml.YAMLError, ValidationError, TypeError) as e:
            raise PersonaError(f"invalid persona {persona_id}: {e}") from e
        if persona.persona_id != persona_id:
            raise PersonaError(f"persona_id {persona.persona_id!r} does not match file {persona_id}.yaml")
        persona.version = str(persona.schema_version)
        self._defaults[persona_id] = persona
        return persona

    def persona_id_for(self, channel_id):
        if channel_id not in PIPELINES:
            raise PersonaError(f"unknown channel/pipeline {channel_id!r}")
        return PIPELINE_PERSONA[channel_id]

    def validate_all(self):
        """Every pipeline must resolve to exactly one valid persona."""
        return {ch: self._read(self.persona_id_for(ch)) for ch in PIPELINES}

    def load(self, tenant_id, channel_id):
        base = self._read(self.persona_id_for(channel_id))
        settings = self.repository.get_settings(tenant_id, channel_id) if self.repository else {}
        overrides = settings.get("persona_overrides") or {}
        if not overrides:
            return base
        return apply_overrides(base, overrides, settings.get("override_rev", 0))


def apply_overrides(base, overrides, revision=1):
    bad = [k for k in overrides if k not in ALLOWED_OVERRIDES]
    if bad:
        raise PersonaError(f"override(s) not allowed: {bad}")
    data = copy.deepcopy(base.model_dump())
    for key, value in overrides.items():
        node = data
        parts = key.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    try:
        persona = PersonaConfig.model_validate(data)
    except ValidationError as e:
        raise PersonaError(f"invalid persona override: {e}") from e
    persona.version = f"{base.schema_version}+o{revision}"
    return persona


def render_persona(persona):
    """Compact prompt text for the persona (not a raw YAML dump)."""
    v, d = persona.voice, persona.domain
    lines = [
        f"Identity: {v.identity}",
        f"Traits: {', '.join(v.traits)}",
        f"Default language: {persona.language}",
        f"Humor {v.humor_level}/5, assertiveness {v.assertiveness}/5, emoji: {v.emoji_level}, slang: {v.slang_level}",
        f"Max sentences: {v.max_sentences}",
    ]
    flags = [k.replace("_", " ") for k, val in d.model_dump().items() if val is True and k != "use_video_context"]
    if flags:
        lines.append("Domain rules: " + "; ".join(flags))
    lines += [f"- {r}" for r in persona.extra_rules]
    return "\n".join(lines)
