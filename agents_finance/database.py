"""Finance state store. Uses DATABASE_URL (same Supabase pooler as other channels) with finance_-prefixed tables,
so nothing is shared with other channels' data. Falls back to in-memory for local/dry runs.
Exposes get_meta/set_meta (the interface RunState needs) plus posted-topic tracking."""
import os


class MemoryStore:
    persistent = False
    def __init__(self): self.meta, self.posted = {}, {}
    def get_meta(self, k): return self.meta.get(k)
    def set_meta(self, k, v): self.meta[k] = v
    def posted_ids(self): return set(self.posted)
    def mark_posted(self, tid, title): self.posted[tid] = title


class PostgresStore:
    persistent = True
    def __init__(self, url):
        try:
            import psycopg2 as drv
            self.conn = drv.connect(url)
        except ImportError:
            import psycopg as drv
            self.conn = drv.connect(url, prepare_threshold=None)   # pgbouncer transaction pooler (port 6543)
        self.conn.autocommit = True
        with self.conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS finance_meta (key text PRIMARY KEY, value text)")
            c.execute("CREATE TABLE IF NOT EXISTS finance_posted (topic_id text PRIMARY KEY, title text, posted_at timestamptz DEFAULT now())")

    def get_meta(self, k):
        with self.conn.cursor() as c:
            c.execute("SELECT value FROM finance_meta WHERE key=%s", (k,)); r = c.fetchone()
        return r[0] if r else None

    def set_meta(self, k, v):
        with self.conn.cursor() as c:
            c.execute("INSERT INTO finance_meta(key,value) VALUES(%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (k, v))

    def posted_ids(self):
        with self.conn.cursor() as c:
            c.execute("SELECT topic_id FROM finance_posted"); return {r[0] for r in c.fetchall()}

    def mark_posted(self, tid, title):
        with self.conn.cursor() as c:
            c.execute("INSERT INTO finance_posted(topic_id,title) VALUES(%s,%s) ON CONFLICT DO NOTHING", (tid, title))


def open_store(env=None):
    e = os.environ if env is None else env
    url = e.get("DATABASE_URL")
    if url:
        return PostgresStore(url)
    print("WARNING: DATABASE_URL not set; Finance state is in-memory and will not survive this run.")
    return MemoryStore()
