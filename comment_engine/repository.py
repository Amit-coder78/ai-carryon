"""Persistence for the comment agent (SQLite locally, Postgres on CI).

Every query is scoped by (tenant_id, channel_id). Provider ids are idempotency
keys, never an authorization boundary. State changes are atomic
(UPDATE ... WHERE status IN (...) RETURNING).
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from .config import STALE_PROCESSING_MINUTES
from .schemas import FEEDBACK_LABELS, Comment

TERMINAL_COMMENT = {"published", "skipped", "blocked"}
PROCESSABLE = ("fetched", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS comment_agent_settings (
    tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL,
    persona_overrides TEXT DEFAULT '{}', override_rev INTEGER DEFAULT 0, faqs TEXT DEFAULT '[]',
    publishing_enabled INTEGER DEFAULT 0, paused INTEGER DEFAULT 0,
    last_sync_at TEXT, updated_at TEXT,
    PRIMARY KEY (tenant_id, channel_id)
);
CREATE TABLE IF NOT EXISTS comment_agent_comments (
    tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, comment_id TEXT NOT NULL,
    video_id TEXT, parent_comment_id TEXT, author_channel_id TEXT, author_name TEXT,
    text TEXT, published_at TEXT, intent TEXT, status TEXT DEFAULT 'fetched',
    attempts INTEGER DEFAULT 0, last_error TEXT, created_at TEXT, updated_at TEXT,
    PRIMARY KEY (tenant_id, channel_id, comment_id)
);
CREATE TABLE IF NOT EXISTS comment_agent_drafts (
    draft_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, comment_id TEXT NOT NULL,
    persona_id TEXT, persona_version TEXT, prompt_version TEXT, model_version TEXT,
    reply_text TEXT, original_reply_text TEXT, strategy TEXT, status TEXT,
    validation_passed INTEGER DEFAULT 0, validation_reasons TEXT DEFAULT '[]',
    review_reason TEXT, feedback TEXT, approved_by TEXT, approved_at TEXT,
    published_reply_id TEXT, last_error TEXT, latency_ms INTEGER,
    created_at TEXT, updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_comment_agent_draft_comment
    ON comment_agent_drafts(tenant_id, channel_id, comment_id);
CREATE INDEX IF NOT EXISTS ix_comment_agent_draft_status
    ON comment_agent_drafts(tenant_id, channel_id, status);
CREATE TABLE IF NOT EXISTS comment_agent_events (
    event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL,
    comment_id TEXT, draft_id TEXT, event_type TEXT, actor TEXT, detail TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_comment_agent_events_scope
    ON comment_agent_events(tenant_id, channel_id, created_at);
CREATE TABLE IF NOT EXISTS comment_agent_videos (
    tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, video_id TEXT NOT NULL,
    title TEXT, description TEXT, transcript TEXT, fetched_at TEXT,
    PRIMARY KEY (tenant_id, channel_id, video_id)
);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _row(r):
    return dict(r) if r is not None else None


class Repository:
    def __init__(self, db=None):
        if db is None:
            from agents.database import db as shared
            db = shared
        self.db = db
        self.ensure_schema()

    def ensure_schema(self):
        with self.db._conn() as conn:
            conn.executescript(SCHEMA)

    # -- settings --------------------------------------------------------
    def get_settings(self, tenant_id, channel_id):
        with self.db._conn() as c:
            r = c.execute("SELECT * FROM comment_agent_settings WHERE tenant_id=? AND channel_id=?",
                          (tenant_id, channel_id)).fetchone()
        if not r:
            return {"persona_overrides": {}, "override_rev": 0, "faqs": [], "publishing_enabled": False,
                    "paused": False, "last_sync_at": None}
        d = dict(r)
        return {"persona_overrides": json.loads(d["persona_overrides"] or "{}"),
                "override_rev": d["override_rev"] or 0, "faqs": json.loads(d["faqs"] or "[]"),
                "publishing_enabled": bool(d["publishing_enabled"]), "paused": bool(d["paused"]),
                "last_sync_at": d["last_sync_at"]}

    def update_settings(self, tenant_id, channel_id, **changes):
        cur = self.get_settings(tenant_id, channel_id)
        if "persona_overrides" in changes:
            cur["override_rev"] += 1
        cur.update(changes)
        with self.db._conn() as c:
            c.execute("""INSERT INTO comment_agent_settings
                (tenant_id, channel_id, persona_overrides, override_rev, faqs, publishing_enabled, paused, last_sync_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, channel_id) DO UPDATE SET
                persona_overrides=excluded.persona_overrides, override_rev=excluded.override_rev, faqs=excluded.faqs,
                publishing_enabled=excluded.publishing_enabled, paused=excluded.paused,
                last_sync_at=excluded.last_sync_at, updated_at=excluded.updated_at""",
                      (tenant_id, channel_id, json.dumps(cur["persona_overrides"]), cur["override_rev"],
                       json.dumps(cur["faqs"]), int(cur["publishing_enabled"]), int(cur["paused"]),
                       cur["last_sync_at"], now()))
        return cur

    # -- comments --------------------------------------------------------
    def upsert_comment(self, tenant_id, channel_id, item):
        """Insert if new. Returns True if created (never overwrites an existing row)."""
        ts = now()
        with self.db._conn() as c:
            r = c.execute("""INSERT INTO comment_agent_comments
                (tenant_id, channel_id, comment_id, video_id, parent_comment_id, author_channel_id, author_name,
                 text, published_at, status, attempts, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'fetched',0,?,?)
                ON CONFLICT(tenant_id, channel_id, comment_id) DO NOTHING RETURNING comment_id""",
                          (tenant_id, channel_id, item["comment_id"], item.get("video_id", ""),
                           item.get("parent_comment_id"), item.get("author_channel_id", ""),
                           item.get("author_name", ""), item.get("text", ""), item.get("published_at", ""),
                           ts, ts)).fetchone()
        return r is not None

    def get_comment(self, tenant_id, channel_id, comment_id):
        with self.db._conn() as c:
            r = c.execute("SELECT * FROM comment_agent_comments WHERE tenant_id=? AND channel_id=? AND comment_id=?",
                          (tenant_id, channel_id, comment_id)).fetchone()
        return Comment(**{k: v for k, v in _row(r).items() if k in Comment.model_fields
                          and v is not None}) if r else None

    def list_comments(self, tenant_id, channel_id, statuses=None, limit=50):
        q, p = "SELECT * FROM comment_agent_comments WHERE tenant_id=? AND channel_id=?", [tenant_id, channel_id]
        if statuses:
            q += f" AND status IN ({','.join('?' for _ in statuses)})"
            p += list(statuses)
        q += " ORDER BY published_at DESC, created_at DESC LIMIT ?"
        p.append(int(limit))
        with self.db._conn() as c:
            return [dict(r) for r in c.execute(q, tuple(p)).fetchall()]

    def known_comment_ids(self, tenant_id, channel_id, ids):
        ids = [i for i in ids if i]
        if not ids:
            return set()
        with self.db._conn() as c:
            rows = c.execute(f"SELECT comment_id FROM comment_agent_comments WHERE tenant_id=? AND channel_id=? "
                             f"AND comment_id IN ({','.join('?' for _ in ids)})",
                             (tenant_id, channel_id, *ids)).fetchall()
        return {r["comment_id"] for r in rows}

    def claim_for_processing(self, tenant_id, channel_id, comment_id):
        """Atomic fetched/failed -> processing (or reclaim a stale processing row)."""
        with self.db._conn() as c:
            r = c.execute("""UPDATE comment_agent_comments SET status='processing', updated_at=?
                WHERE tenant_id=? AND channel_id=? AND comment_id=?
                AND (status IN ('fetched','failed') OR (status='processing' AND updated_at < ?))
                RETURNING comment_id""", (now(), tenant_id, channel_id, comment_id,
                                          _ago(STALE_PROCESSING_MINUTES))).fetchone()
        return r is not None

    def set_comment_status(self, tenant_id, channel_id, comment_id, status, error=None, intent=None,
                           bump_attempts=False):
        with self.db._conn() as c:
            c.execute("""UPDATE comment_agent_comments SET status=?, last_error=?,
                intent=COALESCE(?, intent), attempts=attempts+?, updated_at=?
                WHERE tenant_id=? AND channel_id=? AND comment_id=?""",
                      (status, error, intent, 1 if bump_attempts else 0, now(), tenant_id, channel_id, comment_id))

    # -- drafts ----------------------------------------------------------
    def create_draft(self, tenant_id, channel_id, comment_id, *, reply_text, strategy, status, persona_id,
                     persona_version, prompt_version, validation_passed, validation_reasons=(),
                     review_reason=None, model_version=None, latency_ms=None):
        """One draft per comment (idempotent). Returns draft_id, or None if one exists."""
        draft_id, ts = uuid.uuid4().hex, now()
        with self.db._conn() as c:
            r = c.execute("""INSERT INTO comment_agent_drafts
                (draft_id, tenant_id, channel_id, comment_id, persona_id, persona_version, prompt_version,
                 model_version, reply_text, original_reply_text, strategy, status, validation_passed,
                 validation_reasons, review_reason, latency_ms, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, channel_id, comment_id) DO NOTHING RETURNING draft_id""",
                          (draft_id, tenant_id, channel_id, comment_id, persona_id, persona_version,
                           prompt_version, model_version, reply_text, reply_text, strategy, status,
                           int(validation_passed), json.dumps(list(validation_reasons)), review_reason,
                           latency_ms, ts, ts)).fetchone()
        return draft_id if r else None

    def get_draft(self, tenant_id, channel_id, draft_id):
        with self.db._conn() as c:
            r = c.execute("SELECT * FROM comment_agent_drafts WHERE tenant_id=? AND channel_id=? AND draft_id=?",
                          (tenant_id, channel_id, draft_id)).fetchone()
        return _decode(_row(r))

    def get_draft_for_comment(self, tenant_id, channel_id, comment_id):
        with self.db._conn() as c:
            r = c.execute("SELECT * FROM comment_agent_drafts WHERE tenant_id=? AND channel_id=? AND comment_id=?",
                          (tenant_id, channel_id, comment_id)).fetchone()
        return _decode(_row(r))

    def list_drafts(self, tenant_id, channel_id, statuses=None, limit=50):
        q, p = "SELECT * FROM comment_agent_drafts WHERE tenant_id=? AND channel_id=?", [tenant_id, channel_id]
        if statuses:
            q += f" AND status IN ({','.join('?' for _ in statuses)})"
            p += list(statuses)
        q += " ORDER BY created_at DESC LIMIT ?"
        p.append(int(limit))
        with self.db._conn() as c:
            return [_decode(dict(r)) for r in c.execute(q, tuple(p)).fetchall()]

    def transition_draft(self, tenant_id, channel_id, draft_id, from_statuses, to_status, **fields):
        """Atomic compare-and-set on status. Returns True if the transition happened."""
        allowed = {"reply_text", "validation_passed", "validation_reasons", "review_reason", "approved_by",
                   "approved_at", "published_reply_id", "last_error", "feedback"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown draft fields: {bad}")
        sets, vals = ["status=?", "updated_at=?"], [to_status, now()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(list(v)) if k == "validation_reasons" else (int(v) if k == "validation_passed" else v))
        vals += [tenant_id, channel_id, draft_id, *from_statuses]
        with self.db._conn() as c:
            r = c.execute(f"UPDATE comment_agent_drafts SET {', '.join(sets)} WHERE tenant_id=? AND channel_id=? "
                          f"AND draft_id=? AND status IN ({','.join('?' for _ in from_statuses)}) "
                          f"RETURNING draft_id", tuple(vals)).fetchone()
        return r is not None

    def reclaim_stale_publishing(self, tenant_id, channel_id, draft_id, minutes):
        with self.db._conn() as c:
            r = c.execute("""UPDATE comment_agent_drafts SET status='publish_failed', last_error='stale_publishing',
                updated_at=? WHERE tenant_id=? AND channel_id=? AND draft_id=? AND status='publishing'
                AND updated_at < ? RETURNING draft_id""",
                          (now(), tenant_id, channel_id, draft_id, _ago(minutes))).fetchone()
        return r is not None

    def set_feedback(self, tenant_id, channel_id, draft_id, label):
        if label not in FEEDBACK_LABELS:
            raise ValueError(f"feedback must be one of {FEEDBACK_LABELS}")
        with self.db._conn() as c:
            r = c.execute("UPDATE comment_agent_drafts SET feedback=?, updated_at=? WHERE tenant_id=? AND "
                          "channel_id=? AND draft_id=? RETURNING draft_id",
                          (label, now(), tenant_id, channel_id, draft_id)).fetchone()
        return r is not None

    def recent_reply_texts(self, tenant_id, channel_id, limit, exclude_draft_id=None):
        with self.db._conn() as c:
            rows = c.execute("""SELECT draft_id, reply_text FROM comment_agent_drafts
                WHERE tenant_id=? AND channel_id=? AND status NOT IN ('rejected') AND reply_text <> ''
                ORDER BY created_at DESC LIMIT ?""", (tenant_id, channel_id, int(limit) + 1)).fetchall()
        return [r["reply_text"] for r in rows if r["draft_id"] != exclude_draft_id][:limit]

    def approved_examples(self, tenant_id, channel_id, limit=30):
        """Owner-approved voice examples: drafts marked on_brand that were approved/published."""
        with self.db._conn() as c:
            rows = c.execute("""SELECT d.reply_text AS reply, m.text AS comment
                FROM comment_agent_drafts d JOIN comment_agent_comments m
                  ON m.tenant_id=d.tenant_id AND m.channel_id=d.channel_id AND m.comment_id=d.comment_id
                WHERE d.tenant_id=? AND d.channel_id=? AND d.feedback='on_brand'
                  AND d.status IN ('approved','published') ORDER BY d.updated_at DESC LIMIT ?""",
                             (tenant_id, channel_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    # -- events / video cache / metrics / retention ----------------------
    def record_event(self, tenant_id, channel_id, event_type, *, comment_id=None, draft_id=None,
                     actor="system", detail=None):
        with self.db._conn() as c:
            c.execute("""INSERT INTO comment_agent_events
                (event_id, tenant_id, channel_id, comment_id, draft_id, event_type, actor, detail, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                      (uuid.uuid4().hex, tenant_id, channel_id, comment_id, draft_id, event_type, actor,
                       json.dumps(detail or {}, default=str)[:4000], now()))

    def list_events(self, tenant_id, channel_id, comment_id=None, draft_id=None, limit=100):
        q, p = "SELECT * FROM comment_agent_events WHERE tenant_id=? AND channel_id=?", [tenant_id, channel_id]
        if comment_id:
            q, p = q + " AND comment_id=?", p + [comment_id]
        if draft_id:
            q, p = q + " AND draft_id=?", p + [draft_id]
        with self.db._conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*p, int(limit))).fetchall()]

    def event_exists(self, tenant_id, channel_id, comment_id, event_type):
        with self.db._conn() as c:
            r = c.execute("SELECT 1 AS x FROM comment_agent_events WHERE tenant_id=? AND channel_id=? AND "
                          "comment_id=? AND event_type=? LIMIT 1", (tenant_id, channel_id, comment_id,
                                                                   event_type)).fetchone()
        return r is not None

    def get_video(self, tenant_id, channel_id, video_id):
        with self.db._conn() as c:
            r = c.execute("SELECT * FROM comment_agent_videos WHERE tenant_id=? AND channel_id=? AND video_id=?",
                          (tenant_id, channel_id, video_id)).fetchone()
        return _row(r)

    def put_video(self, tenant_id, channel_id, video_id, title="", description="", transcript=""):
        with self.db._conn() as c:
            c.execute("""INSERT INTO comment_agent_videos
                (tenant_id, channel_id, video_id, title, description, transcript, fetched_at) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, channel_id, video_id) DO UPDATE SET title=excluded.title,
                description=excluded.description, transcript=excluded.transcript, fetched_at=excluded.fetched_at""",
                      (tenant_id, channel_id, video_id, title, description, transcript, now()))

    def metrics(self, tenant_id, channel_id):
        with self.db._conn() as c:
            rows = c.execute("SELECT status, COUNT(*) AS n FROM comment_agent_drafts WHERE tenant_id=? AND "
                             "channel_id=? GROUP BY status", (tenant_id, channel_id)).fetchall()
            edited = c.execute("SELECT COUNT(*) AS n FROM comment_agent_drafts WHERE tenant_id=? AND channel_id=? "
                               "AND reply_text <> original_reply_text", (tenant_id, channel_id)).fetchone()["n"]
            fb = c.execute("SELECT feedback, COUNT(*) AS n FROM comment_agent_drafts WHERE tenant_id=? AND "
                           "channel_id=? AND feedback IS NOT NULL GROUP BY feedback", (tenant_id, channel_id)).fetchall()
        return {"drafts_by_status": {r["status"]: r["n"] for r in rows}, "edited": edited,
                "feedback": {r["feedback"]: r["n"] for r in fb}}

    def purge_older_than(self, tenant_id, channel_id, days):
        """Retention: delete finished comments/drafts/events older than `days` days."""
        cutoff = _ago(days * 24 * 60)
        with self.db._conn() as c:
            c.execute("DELETE FROM comment_agent_drafts WHERE tenant_id=? AND channel_id=? AND updated_at < ? "
                      "AND status IN ('published','rejected','skipped','blocked')", (tenant_id, channel_id, cutoff))
            c.execute("DELETE FROM comment_agent_comments WHERE tenant_id=? AND channel_id=? AND updated_at < ? "
                      "AND status IN ('published','skipped','blocked')", (tenant_id, channel_id, cutoff))
            c.execute("DELETE FROM comment_agent_events WHERE tenant_id=? AND channel_id=? AND created_at < ?",
                      (tenant_id, channel_id, cutoff))


def _decode(d):
    if d is None:
        return None
    d["validation_reasons"] = json.loads(d.get("validation_reasons") or "[]")
    d["validation_passed"] = bool(d.get("validation_passed"))
    return d
