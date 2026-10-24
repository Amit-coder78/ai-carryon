"""
brain_client.py — client for the AI CarryON Brain platform (ai-carryon-brain-platform).

Asks the Brain engine (`POST /v1/runtime/execute`) to rank candidate topics. This pipeline runs as
short, stateless GitHub Actions jobs, so there is no circuit breaker (it would reset every run);
instead a hard, short timeout bounds the cost of a dead engine to a couple of seconds, once.

Rules:
  1. Never raises. Any failure returns BrainResult(applied=False, reason=...) and the caller keeps
     its existing topic selection.
  2. `source` is exactly what the engine reported (brains | partial | baseline_fallback), or
     `engine_unavailable` / `disabled` — a failed call is never presented as a Brain result.
  3. A ranking only counts if it discriminates: when every candidate gets the same fused score the
     engine has no preference, and that is reported as `no_signal`, not applied.

Configuration (env; all unset => feature fully off, zero network calls, zero behaviour change):
  BRAIN_ENGINE_URL            e.g. https://brain.example.com  (must be reachable from the runner)
  BRAIN_WORKSPACE_ID          workspace uuid in the Brain registry (sent as x-workspace-id)
  BRAIN_CHANNEL_ID_<CHANNEL>  registry channel uuid, e.g. BRAIN_CHANNEL_ID_ENGLISH
  BRAIN_ENABLED               "0"/"false" kill switch
  BRAIN_BRAIN_TIMEOUT_MS      per-Brain timeout inside the engine (default 1500)
  BRAIN_HTTP_MARGIN_MS        extra HTTP time on top of that (default 1000)
"""
import os
import time
from dataclasses import dataclass, field
from uuid import UUID

import requests

MAX_CANDIDATES = 200
MAX_TITLE_CHARS = 300
FEATURE_KEYS = ("unique_requesters", "similar_requests", "days_since_trend", "channel_baseline_ratio")
_INT_KEYS = ("unique_requesters", "similar_requests")


@dataclass(frozen=True)
class BrainResult:
    source: str          # brains | partial | baseline_fallback | engine_unavailable | disabled
    applied: bool        # True only if `ordered` should drive topic choice
    reason: str          # "ok" or why not applied
    ordered: list = field(default_factory=list)          # topics best-first (only when applied)
    execution_id: str = None
    policy_version: str = None
    brain_versions: list = field(default_factory=list)   # [{brain_id, brain_version_id, role, status}]
    topics_sent: int = 0
    duration_ms: int = 0

    def to_log_dict(self):
        return {"source": self.source, "applied": self.applied, "reason": self.reason,
                "execution_id": self.execution_id, "policy_version": self.policy_version,
                "brain_versions": self.brain_versions, "topics_sent": self.topics_sent,
                "duration_ms": self.duration_ms}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _as_uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _settings(channel):
    """(base_url, workspace_uuid, channel_uuid, skip_reason). skip_reason is None when fully configured."""
    if os.environ.get("BRAIN_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return None, None, None, "kill_switch"
    base = (os.environ.get("BRAIN_ENGINE_URL") or "").strip().rstrip("/")
    if not base:
        return None, None, None, "engine_url_not_set"
    ch = _as_uuid(os.environ.get(f"BRAIN_CHANNEL_ID_{channel.upper()}"))
    if ch is None:
        return None, None, None, "channel_not_linked"
    ws = _as_uuid(os.environ.get("BRAIN_WORKSPACE_ID"))
    if ws is None:
        return None, None, None, "workspace_not_set"
    return base, ws, ch, None


def is_configured(channel):
    """Cheap env-only check, so callers can skip candidate-gathering work when the feature is off."""
    return _settings(channel)[3] is None


def clean_features(raw):
    """Keep only known, finite, non-negative numbers; the engine rejects anything else."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in FEATURE_KEYS:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if v != v or v in (float("inf"), float("-inf")) or v < 0:
            continue
        out[key] = int(v) if key in _INT_KEYS else float(v)
    return out


def _skip(source, reason, sent=0):
    return BrainResult(source=source, applied=False, reason=reason, topics_sent=sent)


def rank_topics(topics, channel, features=None, post=None):
    """Rank `topics` (list of str) for `channel`. `features` maps topic -> signals dict.
    `post` is an injectable requests.post for tests. Never raises."""
    base, ws, ch, why = _settings(channel)
    if why:
        return _skip("disabled", why)

    unique, seen = [], set()
    for t in topics:
        if isinstance(t, str) and t.strip() and t not in seen:
            seen.add(t)
            unique.append(t)
    unique = unique[:MAX_CANDIDATES]
    if len(unique) < 2:
        return _skip("disabled", "fewer_than_two_candidates", len(unique))

    id_to_topic = {f"t{i}": t for i, t in enumerate(unique)}
    candidates = []
    for cid, t in id_to_topic.items():
        cand = {"candidate_id": cid, "title": t[:MAX_TITLE_CHARS]}
        feats = clean_features((features or {}).get(t))
        if feats:
            cand["features"] = feats
        candidates.append(cand)

    brain_ms = _env_int("BRAIN_BRAIN_TIMEOUT_MS", 1500)
    total_s = (brain_ms + _env_int("BRAIN_HTTP_MARGIN_MS", 1000)) / 1000.0
    payload = {"channel_id": ch, "candidates": candidates, "options": {"timeout_ms": brain_ms}}

    t0 = time.perf_counter()
    try:
        resp = (post or requests.post)(f"{base}/v1/runtime/execute", json=payload,
                                       headers={"x-workspace-id": ws}, timeout=(2.0, total_s))
        if resp.status_code != 200:
            raise RuntimeError(f"engine returned HTTP {resp.status_code}")
        result = _interpret(resp.json(), id_to_topic, len(unique))
    except Exception as exc:
        return BrainResult(source="engine_unavailable", applied=False,
                           reason=f"call_failed:{type(exc).__name__}", topics_sent=len(unique),
                           duration_ms=int((time.perf_counter() - t0) * 1000))
    return BrainResult(**{**result.__dict__, "duration_ms": int((time.perf_counter() - t0) * 1000)})


def _interpret(body, id_to_topic, sent):
    source = body["source"]
    if source not in ("brains", "partial", "baseline_fallback"):
        raise ValueError(f"unknown source {source!r}")
    meta = dict(
        source=source,
        execution_id=str(body["execution_id"]) if body.get("execution_id") else None,
        policy_version=body.get("policy_version"),
        brain_versions=[{"brain_id": b.get("brain_id"), "brain_version_id": b.get("brain_version_id"),
                         "role": b.get("role"), "status": b.get("status")} for b in body.get("brains", [])],
        topics_sent=sent,
    )
    if source == "baseline_fallback":
        return BrainResult(applied=False, reason="engine_baseline_fallback", **meta)
    ranked = [r for r in body.get("ranked", []) if r.get("candidate_id") in id_to_topic]
    if len({round(float(r.get("aggregate_score", 0.0)), 8) for r in ranked}) <= 1:
        return BrainResult(applied=False, reason="no_signal", **meta)
    ids = [r["candidate_id"] for r in ranked]
    ids += [c for c in id_to_topic if c not in set(ids)]
    return BrainResult(applied=True, reason="ok", ordered=[id_to_topic[c] for c in ids], **meta)
