"""
agents/database.py — Central database for AI CarryON

Supports two backends, chosen automatically:
  - Postgres (if DATABASE_URL is set) — for production / GitHub Actions,
    where the filesystem is wiped between runs and a persistent external
    database is required. Use a free Postgres from Supabase or Neon.
  - SQLite (default, if DATABASE_URL is not set) — for local development,
    stored at output/aicarryon.db.

Tables:
  - videos          : YouTube video metadata
  - snapshots       : Hourly view/like counts per video
  - ab_title_tests  : A/B title test results
  - posted_topics   : Topics already posted (deduplication)
  - spy_cache       : Trending topic cache
  - locks           : Simple cross-run generation locks (try_acquire_lock/release_lock)

Usage:
    from agents.database import db
    db.add_snapshot(video_id, views, likes)
    snapshots = db.get_snapshots(video_id)
"""

import os
import json
from datetime import datetime, timezone
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3


def _parse_ts_safe(ts_str):
    """
    Parse a timestamp that may be naive or timezone-aware, always
    return a timezone-AWARE UTC datetime. This fixes the bug where
    some snapshots were saved with utcnow() (naive) and others with
    datetime.now(timezone.utc) (aware), causing subtraction to fail.
    """
    from datetime import datetime as _dt, timezone as _tz
    s = ts_str.replace("Z", "+00:00")
    parsed = _dt.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz.utc)
    return parsed


def _to_pg(query):
    """Translate sqlite-style '?' placeholders to psycopg2-style '%s'."""
    return query.replace("?", "%s")


class _CursorWrapper:
    """Makes a psycopg2 cursor's .execute() behave like sqlite3's
    connection-level .execute() shortcut, so the rest of this file can use
    one code path (conn.execute(...).fetchone()/.fetchall()) either way."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        self._cursor.execute(_to_pg(query), params or ())
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _ConnWrapper:
    def __init__(self, conn, is_pg):
        self._conn = conn
        self._is_pg = is_pg

    def execute(self, query, params=None):
        if self._is_pg:
            cur = self._conn.cursor()
            return _CursorWrapper(cur).execute(query, params)
        else:
            return self._conn.execute(query, params or ())

    def executescript(self, script):
        if self._is_pg:
            cur = self._conn.cursor()
            cur.execute(script)
        else:
            self._conn.executescript(script)


# Store DB in output/ folder for local/SQLite mode only
DB_PATH = os.environ.get("DB_PATH", "output/aicarryon.db")


class Database:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        if not USE_POSTGRES:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_tables()

    @contextmanager
    def _conn(self):
        if USE_POSTGRES:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")  # better concurrent access
        try:
            yield _ConnWrapper(conn, USE_POSTGRES)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self):
        """Create tables if they don't exist. Postgres and SQLite use
        different auto-increment syntax, so the schema is picked per backend;
        everything else (columns, ON CONFLICT clauses) is identical."""
        id_col = "id SERIAL PRIMARY KEY" if USE_POSTGRES else "id INTEGER PRIMARY KEY AUTOINCREMENT"

        with self._conn() as conn:
            conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS videos (
                    video_id    TEXT PRIMARY KEY,
                    title       TEXT,
                    published   TEXT,
                    channel     TEXT DEFAULT 'english',
                    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    {id_col},
                    video_id    TEXT NOT NULL,
                    views       INTEGER DEFAULT 0,
                    likes       INTEGER DEFAULT 0,
                    comments    INTEGER DEFAULT 0,
                    timestamp   TEXT NOT NULL,
                    FOREIGN KEY (video_id) REFERENCES videos(video_id)
                );

                CREATE TABLE IF NOT EXISTS ab_title_tests (
                    {id_col},
                    topic              TEXT,
                    channel            TEXT DEFAULT 'english',
                    winner_title       TEXT,
                    winner_pattern     TEXT,
                    winner_score       INTEGER,
                    all_variations     TEXT,
                    generated_at       TEXT,
                    actual_views       INTEGER DEFAULT NULL,
                    actual_views_24h   INTEGER DEFAULT NULL,
                    actual_checked_at  TEXT DEFAULT NULL,
                    video_id           TEXT DEFAULT NULL
                );

                CREATE TABLE IF NOT EXISTS posted_topics (
                    {id_col},
                    topic       TEXT NOT NULL,
                    channel     TEXT DEFAULT 'english',
                    posted_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS spy_cache (
                    {id_col},
                    channel     TEXT NOT NULL,
                    topics      TEXT NOT NULL,
                    cached_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS locks (
                    name         TEXT PRIMARY KEY,
                    acquired_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key    TEXT PRIMARY KEY,
                    value  TEXT
                );

                CREATE TABLE IF NOT EXISTS comment_history (
                    comment_id        TEXT PRIMARY KEY,
                    channel           TEXT DEFAULT 'english',
                    video_id          TEXT,
                    username          TEXT,
                    original_comment  TEXT,
                    category          TEXT,
                    generated_reply   TEXT,
                    status            TEXT DEFAULT 'replied',
                    attempts          INTEGER DEFAULT 0,
                    last_error        TEXT,
                    timestamp         TEXT
                );

                CREATE TABLE IF NOT EXISTS topic_requests (
                    {id_col},
                    channel     TEXT DEFAULT 'english',
                    topic       TEXT,
                    comment     TEXT,
                    video_id    TEXT,
                    timestamp   TEXT,
                    author_channel_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_comment_history_channel
                    ON comment_history(channel, timestamp);
                CREATE INDEX IF NOT EXISTS idx_topic_requests_channel
                    ON topic_requests(channel, timestamp);

                CREATE INDEX IF NOT EXISTS idx_snapshots_video_id
                    ON snapshots(video_id);
                CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
                    ON snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_posted_topics_channel
                    ON posted_topics(channel, posted_at);
            """)
        self._migrate_ab_title_tests_columns()
        self._migrate_topic_requests_columns()

    def _migrate_topic_requests_columns(self):
        """Adds topic_requests.author_channel_id to older databases (nullable; old rows stay NULL,
        which the Brain demand signal treats as 'unknown person', never as a distinct requester)."""
        with self._conn() as conn:
            if USE_POSTGRES:
                rows = conn.execute("""
                    SELECT column_name AS name FROM information_schema.columns
                    WHERE table_name = 'topic_requests'
                """).fetchall()
            else:
                rows = conn.execute("PRAGMA table_info(topic_requests)").fetchall()
            if "author_channel_id" not in [row["name"] for row in rows]:
                conn.execute("ALTER TABLE topic_requests ADD COLUMN author_channel_id TEXT")
                print("Migrated topic_requests: added column author_channel_id")

    def _migrate_ab_title_tests_columns(self):
        """
        Self-healing migration: adds columns to ab_title_tests if an
        existing (older) database is missing them. Runs on every startup,
        safe to run repeatedly — a fresh CREATE TABLE above already has
        these columns, so this is a no-op for new databases.
        """
        with self._conn() as conn:
            if USE_POSTGRES:
                rows = conn.execute("""
                    SELECT column_name AS name FROM information_schema.columns
                    WHERE table_name = 'ab_title_tests'
                """).fetchall()
            else:
                rows = conn.execute("PRAGMA table_info(ab_title_tests)").fetchall()
            existing = [row["name"] for row in rows]
            additions = {
                "actual_views_24h": "INTEGER DEFAULT NULL",
                "actual_checked_at": "TEXT DEFAULT NULL",
                "video_id": "TEXT DEFAULT NULL",
                "channel": "TEXT DEFAULT 'english'",
            }
            for col, decl in additions.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE ab_title_tests ADD COLUMN {col} {decl}")
                    print(f"Migrated ab_title_tests: added column {col}")

            # One-time self-heal (safe to repeat): Hindi/Bhakti rows were saved
            # without a channel, so they defaulted to 'english'. Their topics
            # carry a prefix, which tells us the true channel.
            try:
                conn.execute("UPDATE ab_title_tests SET channel = 'hindi' "
                             "WHERE channel = 'english' AND substr(topic, 1, 5) = '[HI] '")
                conn.execute("UPDATE ab_title_tests SET channel = 'bhakti' "
                             "WHERE channel = 'english' AND substr(topic, 1, 9) = '[BHAKTI] '")
            except Exception as e:
                print(f"ab_title_tests channel repair skipped: {e}")

    # ── Locks ─────────────────────────────────────────────────────────────
    # Used by scheduler.py / scheduler_hindi.py to avoid two overlapping
    # runs (e.g. a slow run still going when the next cron trigger fires)
    # from generating/uploading the same slot twice. Backed by a plain
    # table + TTL rather than a real distributed lock — good enough for
    # "at most one generation job at a time," which is all this needs.

    def try_acquire_lock(self, name, ttl_seconds=1800):
        """Returns (acquired: bool, age_seconds: float).
        If no lock row exists, or the existing one is older than
        ttl_seconds (meaning a previous run likely crashed without
        releasing it), acquires/steals the lock and returns True."""
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT acquired_at FROM locks WHERE name = ?", (name,)
            ).fetchone()
            age = 0.0
            if row:
                acquired_at = _parse_ts_safe(row["acquired_at"])
                age = (now - acquired_at).total_seconds()
                if age <= ttl_seconds:
                    return False, age
            conn.execute("""
                INSERT INTO locks (name, acquired_at) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET acquired_at = excluded.acquired_at
            """, (name, now.isoformat()))
            return True, age

    def release_lock(self, name):
        with self._conn() as conn:
            conn.execute("DELETE FROM locks WHERE name = ?", (name,))

    # ── Videos ────────────────────────────────────────────────────────────

    def upsert_video(self, video_id, title, published, channel="english"):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO videos (video_id, title, published, channel)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    title=excluded.title,
                    published=excluded.published
            """, (video_id, title, published, channel))

    def get_video(self, video_id):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_videos(self, channel=None):
        with self._conn() as conn:
            if channel:
                rows = conn.execute(
                    "SELECT * FROM videos WHERE channel = ?", (channel,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM videos").fetchall()
            return [dict(r) for r in rows]

    # ── Snapshots ──────────────────────────────────────────────────────────

    def add_snapshot(self, video_id, views, likes=0, comments=0, timestamp=None):
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO snapshots (video_id, views, likes, comments, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (video_id, views, likes, comments, timestamp))

    def get_snapshots(self, video_id):
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM snapshots
                WHERE video_id = ?
                ORDER BY timestamp ASC
            """, (video_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_all_snapshots(self, channel=None):
        """Return all snapshots grouped by video_id — same format as view_history.json.
        If channel is given, only videos for that channel are included."""
        with self._conn() as conn:
            if channel:
                videos = conn.execute("SELECT * FROM videos WHERE channel = ?", (channel,)).fetchall()
            else:
                videos = conn.execute("SELECT * FROM videos").fetchall()
            result = {}
            for video in videos:
                vid_id = video["video_id"]
                snapshots = conn.execute("""
                    SELECT views, likes, comments, timestamp
                    FROM snapshots WHERE video_id = ?
                    ORDER BY timestamp ASC
                """, (vid_id,)).fetchall()
                result[vid_id] = {
                    "title": video["title"],
                    "published": video["published"],
                    "channel": video["channel"],
                    "snapshots": [dict(s) for s in snapshots],
                }
            return result

    # ── A/B Title Tests ────────────────────────────────────────────────────

    def log_ab_test(self, topic, winner_title, winner_pattern, winner_score,
                    all_variations, generated_at=None, channel="english"):
        if generated_at is None:
            generated_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO ab_title_tests
                (topic, winner_title, winner_pattern, winner_score, all_variations, generated_at, channel)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (topic, winner_title, winner_pattern, winner_score,
                  json.dumps(all_variations), generated_at, channel))

    def link_ab_test_to_video(self, winner_title, video_id):
        """
        Link the most recent unlinked ab_title_tests row matching this
        winner_title to the freshly uploaded video_id. Called right after
        a successful upload, when both values are available in the same run.
        """
        with self._conn() as conn:
            row = conn.execute("""
                SELECT id FROM ab_title_tests
                WHERE winner_title = ? AND video_id IS NULL
                ORDER BY generated_at DESC LIMIT 1
            """, (winner_title,)).fetchone()
            if row:
                conn.execute("""
                    UPDATE ab_title_tests SET video_id = ? WHERE id = ?
                """, (video_id, row["id"]))
                return True
            return False

    def get_ab_tests(self, limit=200):
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM ab_title_tests
                ORDER BY generated_at DESC LIMIT ?
            """, (limit,)).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["all_variations"] = json.loads(d["all_variations"] or "[]")
                result.append(d)
            return result

    def update_ab_actual_views(self, test_id, actual_views):
        """Update with real YouTube views after 24h — for Phase 5 learning."""
        with self._conn() as conn:
            conn.execute("""
                UPDATE ab_title_tests SET actual_views = ?
                WHERE id = ?
            """, (actual_views, test_id))

    # ── Posted Topics ──────────────────────────────────────────────────────

    def mark_posted(self, topic, channel="english", posted_at=None):
        if posted_at is None:
            posted_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO posted_topics (topic, channel, posted_at)
                VALUES (?, ?, ?)
            """, (topic, channel, posted_at))

    def get_recent_posted(self, hours=24, channel="english"):
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT topic FROM posted_topics
                WHERE channel = ? AND posted_at >= ?
            """, (channel, cutoff)).fetchall()
            return [r["topic"].lower().strip() for r in rows]

    # ── Spy Cache ──────────────────────────────────────────────────────────

    def save_spy_cache(self, channel, topics):
        cached_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO spy_cache (channel, topics, cached_at)
                VALUES (?, ?, ?)
            """, (channel, json.dumps(topics), cached_at))

    def get_spy_cache(self, channel, max_age_seconds=21600):
        with self._conn() as conn:
            row = conn.execute("""
                SELECT * FROM spy_cache
                WHERE channel = ?
                ORDER BY cached_at DESC LIMIT 1
            """, (channel,)).fetchone()
            if not row:
                return None
            from datetime import datetime as dt
            cached_dt = dt.fromisoformat(row["cached_at"])
            age = (datetime.now(timezone.utc) - cached_dt.replace(
                tzinfo=timezone.utc)).total_seconds()
            if age > max_age_seconds:
                return None
            return json.loads(row["topics"])

    # ── Migration from JSON (legacy) ─────────────────────────────────────

    def migrate_from_json(self, view_history_path="output/view_history.json",
                          ab_log_path="output/title_ab_log.json",
                          posted_path="output/posted_topics.txt"):
        """
        One-time migration from JSON files to the database.
        Safe to run multiple times — won't duplicate data.
        """
        migrated = {"videos": 0, "snapshots": 0, "ab_tests": 0, "posted": 0}

        if os.path.exists(view_history_path):
            try:
                with open(view_history_path) as f:
                    view_history = json.load(f)
                for video_id, data in view_history.items():
                    self.upsert_video(
                        video_id,
                        data.get("title", ""),
                        data.get("published", ""),
                    )
                    migrated["videos"] += 1
                    for snap in data.get("snapshots", []):
                        self.add_snapshot(
                            video_id,
                            snap.get("views", 0),
                            snap.get("likes", 0),
                            snap.get("comments", 0),
                            snap.get("timestamp"),
                        )
                        migrated["snapshots"] += 1
                print(f"Migrated {migrated['videos']} videos, {migrated['snapshots']} snapshots")
            except Exception as e:
                print(f"view_history migration error: {e}")

        if os.path.exists(ab_log_path):
            try:
                with open(ab_log_path) as f:
                    ab_logs = json.load(f)
                for entry in ab_logs:
                    winner = entry.get("winner", {})
                    self.log_ab_test(
                        entry.get("topic", ""),
                        winner.get("title", ""),
                        winner.get("pattern", ""),
                        winner.get("score", 0),
                        entry.get("variations", []),
                        entry.get("generated_at"),
                    )
                    migrated["ab_tests"] += 1
                print(f"Migrated {migrated['ab_tests']} A/B tests")
            except Exception as e:
                print(f"AB log migration error: {e}")

        if os.path.exists(posted_path):
            try:
                with open(posted_path) as f:
                    for line in f:
                        line = line.strip()
                        if "|" in line:
                            ts, topic = line.split("|", 1)
                            self.mark_posted(topic.strip(), posted_at=ts.strip())
                            migrated["posted"] += 1
                print(f"Migrated {migrated['posted']} posted topics")
            except Exception as e:
                print(f"Posted topics migration error: {e}")

        return migrated

    def migrate_from_sqlite(self, old_db_path):
        """
        One-time migration: copies every row from an existing standalone
        SQLite aicarryon.db (e.g. pulled off Railway before decommissioning
        it) into whichever backend this Database instance is using —
        typically Postgres. Run this once, locally, before switching the
        live workflow over, so view history / AB test data isn't lost.

        Safe to run against an empty target. Do NOT run twice against a
        target that already has this data — it will duplicate rows, since
        there's no natural unique key on snapshots/ab_title_tests/posted_topics
        to dedupe against.
        """
        import sqlite3 as _sqlite3
        src = _sqlite3.connect(old_db_path)
        src.row_factory = _sqlite3.Row

        tables = [
            ("videos", ["video_id", "title", "published", "channel"]),
            ("snapshots", ["video_id", "views", "likes", "comments", "timestamp"]),
            ("ab_title_tests", ["topic", "winner_title", "winner_pattern", "winner_score",
                                 "all_variations", "generated_at", "actual_views",
                                 "actual_views_24h", "actual_checked_at", "video_id"]),
            ("posted_topics", ["topic", "channel", "posted_at"]),
            ("spy_cache", ["channel", "topics", "cached_at"]),
        ]
        counts = {}
        for table, cols in tables:
            try:
                rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            except _sqlite3.OperationalError:
                counts[table] = 0
                continue
            with self._conn() as conn:
                for r in rows:
                    placeholders = ", ".join(["?"] * len(cols))
                    conn.execute(
                        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                        tuple(r[c] for c in cols),
                    )
            counts[table] = len(rows)
            print(f"Migrated {len(rows)} rows into {table}")
        src.close()
        return counts

    # ── Analytics ──────────────────────────────────────────────────────────

    def get_peak_hours(self, channel=None):
        """
        Calculate peak upload hours from snapshot velocity data.
        Returns dict {hour: avg_velocity} based on real view data.
        If channel is given, only that channel's videos are considered.
        """
        all_data = self.get_all_snapshots(channel=channel)

        from collections import defaultdict
        hour_velocities = defaultdict(list)

        for video_id, data in all_data.items():
            snapshots = data["snapshots"]
            if len(snapshots) < 2:
                continue
            for i in range(1, len(snapshots)):
                prev = snapshots[i-1]
                curr = snapshots[i]
                try:
                    t1 = _parse_ts_safe(prev["timestamp"])
                    t2 = _parse_ts_safe(curr["timestamp"])
                    hours_elapsed = (t2 - t1).total_seconds() / 3600
                    if hours_elapsed <= 0:
                        continue
                    views_gained = max(curr["views"] - prev["views"], 0)
                    velocity = views_gained / hours_elapsed
                    hour_velocities[t2.hour].append(velocity)
                except Exception:
                    continue

        result = {}
        for hour in range(24):
            values = hour_velocities.get(hour, [])
            result[hour] = {
                "avg_velocity": round(sum(values)/len(values), 4) if values else 0.0,
                "sample_count": len(values),
            }
        return result

    def get_best_upload_hour(self):
        """Return the single best hour to upload based on velocity data."""
        peak_hours = self.get_peak_hours()
        best = max(peak_hours.items(), key=lambda x: x[1]["avg_velocity"])
        if best[1]["sample_count"] < 3:
            return None  # not enough data
        return best[0]


    def get_pending_ab_tests(self, channel=None, hours=None):
        """
        Get AB tests that don't have actual_views_24h yet — for closing
        the loop. Matches how close_ab_loop.py actually calls this: with
        no arguments, returning every unclosed row across all channels
        (close_ab_loop.py does its own per-row channel filtering when
        matching against a video).
        """
        try:
            query = "SELECT * FROM ab_title_tests WHERE actual_views_24h IS NULL"
            params = []
            if channel:
                query += " AND channel = ?"
                params.append(channel)
            if hours:
                from datetime import timedelta
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
                query += " AND generated_at >= ?"
                params.append(cutoff)
            with self._conn() as conn:
                rows = conn.execute(query, tuple(params)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            print(f"get_pending_ab_tests error: {e}")
            return []

    def get_meta(self, key):
        """Get a metadata value by key."""
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
                return row["value"] if row else None
        except Exception as e:
            print(f"get_meta error: {e}")
            return None

    def set_meta(self, key, value):
        """Set a metadata value by key."""
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO meta (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """, (key, value))
        except Exception as e:
            print(f"set_meta error: {e}")

    def get_video_by_title(self, title, channel=None):
        """Find a video by its title, optionally filtered by channel."""
        try:
            query = "SELECT * FROM videos WHERE title = ?"
            params = [title]
            if channel:
                query += " AND channel = ?"
                params.append(channel)
            query += " ORDER BY created_at DESC LIMIT 1"
            with self._conn() as conn:
                row = conn.execute(query, tuple(params)).fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"get_video_by_title error: {e}")
            return None

    def set_ab_test_video_id(self, test_id, video_id):
        """Backfill video_id onto an AB test row."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE ab_title_tests SET video_id = ? WHERE id = ?",
                    (video_id, test_id)
                )
        except Exception as e:
            print(f"set_ab_test_video_id error: {e}")

    def close_ab_test(self, test_id, actual_views, closed_at):
        """Fill in actual_views_24h and actual_checked_at on an AB test row."""
        try:
            with self._conn() as conn:
                conn.execute("""
                    UPDATE ab_title_tests
                    SET actual_views_24h = ?, actual_checked_at = ?
                    WHERE id = ?
                """, (actual_views, closed_at, test_id))
        except Exception as e:
            print(f"close_ab_test error: {e}")


    # ── Comment replies (English / Hindi; partitioned by channel) ────────
    # Replaces output/comment_history*.json and output/topic_requests*.json,
    # which were wiped on every GitHub Actions run.

    def get_comments(self, comment_ids):
        """{comment_id: {"status":..., "attempts":...}} for the ids we know."""
        ids = [c for c in comment_ids if c]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT comment_id, status, attempts FROM comment_history "
                f"WHERE comment_id IN ({placeholders})", tuple(ids)
            ).fetchall()
            return {r["comment_id"]: {"status": r["status"], "attempts": r["attempts"] or 0}
                    for r in rows}

    def upsert_comment(self, comment_id, channel, video_id, username, original_comment,
                       category, generated_reply, status, attempts=0, last_error=None):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO comment_history
                (comment_id, channel, video_id, username, original_comment, category,
                 generated_reply, status, attempts, last_error, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(comment_id) DO UPDATE SET
                    category = excluded.category,
                    generated_reply = excluded.generated_reply,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    last_error = excluded.last_error,
                    timestamp = excluded.timestamp
            """, (comment_id, channel, video_id, username, original_comment, category,
                  generated_reply, status, attempts, last_error, now))

    def get_comment_history(self, channel=None, limit=200):
        query = "SELECT * FROM comment_history"
        params = []
        if channel:
            query += " WHERE channel = ?"
            params.append(channel)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query, tuple(params)).fetchall()]

    def save_topic_request(self, channel, topic, comment, video_id, author_channel_id=None):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO topic_requests (channel, topic, comment, video_id, timestamp, author_channel_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (channel, topic, comment, video_id, now, author_channel_id))

    def get_topic_requests(self, channel=None, limit=100):
        query = "SELECT * FROM topic_requests"
        params = []
        if channel:
            query += " WHERE channel = ?"
            params.append(channel)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query, tuple(params)).fetchall()]


# Singleton instance
db = Database()
