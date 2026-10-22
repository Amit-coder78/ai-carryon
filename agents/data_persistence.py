# agents/data_persistence.py
import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
REPO = os.getenv("GITHUB_REPO", "Unknown183-a/ai-carryon")
BRANCH = os.getenv("GITHUB_DATA_BRANCH", "data")

HISTORY_FILE = "output/view_history.json"
AB_LOG_FILE = "output/title_ab_log.json"
POSTED_FILE = "output/posted_topics.txt"
DB_FILE = "output/aicarryon.db"


def _api_base(repo_filename):
    return f"https://api.github.com/repos/{REPO}/contents/{repo_filename}"


def _raw_url(repo_filename, branch=None):
    return f"https://raw.githubusercontent.com/{REPO}/{branch or BRANCH}/{repo_filename}"


def _restore_file(repo_filename, local_path, binary=False, branch=None):
    """Generic: pull a file from a GitHub branch to local_path."""
    try:
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        url = f"{_raw_url(repo_filename, branch)}?nocache={os.urandom(4).hex()}"
        urllib.request.urlretrieve(url, local_path)
        return True
    except Exception as e:
        print(f"No existing backup for {repo_filename} on branch {branch or BRANCH} (starting fresh): {e}")
        return False


def _backup_file(local_path, repo_filename, commit_message):
    """Generic: push a local file to the GitHub data branch via Contents API."""
    if not GITHUB_TOKEN:
        print(f"No GITHUB_TOKEN — skipping backup of {repo_filename}")
        return False

    if not os.path.exists(local_path):
        print(f"No local file at {local_path} — skipping backup")
        return False

    try:
        with open(local_path, "rb") as f:
            raw = f.read()
        content_b64 = base64.b64encode(raw).decode("utf-8")

        sha = None
        try:
            req = urllib.request.Request(
                f"{_api_base(repo_filename)}?ref={BRANCH}",
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.urlopen(req) as resp:
                file_info = json.loads(resp.read())
                sha = file_info.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

        payload = {
            "message": commit_message,
            "content": content_b64,
            "branch": BRANCH,
        }
        if sha:
            payload["sha"] = sha

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _api_base(repo_filename),
            data=data,
            method="PUT",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())
            print(f"{repo_filename} backed up to GitHub ✅ ({len(raw)} bytes)")
            return True

    except Exception as e:
        print(f"Backup of {repo_filename} failed: {e}")
        return False


# ── view_history.json (unchanged behavior, now via shared helpers) ────────

def restore_view_history():
    print("Restoring view history from GitHub...")
    ok = _restore_file("view_history.json", HISTORY_FILE)
    if ok:
        try:
            data = json.load(open(HISTORY_FILE))
            total = sum(len(v["snapshots"]) for v in data.values())
            print(f"Restored {len(data)} videos, {total} total snapshots")
        except Exception as e:
            print(f"Restored file but could not parse summary: {e}")
    return ok


def backup_view_history():
    return _backup_file(HISTORY_FILE, "view_history.json", "Auto: update view history")


# ── title_ab_log.json (NEW — previously never backed up) ──────────────────

def restore_ab_log():
    print("Restoring AB title log from GitHub...")
    return _restore_file("title_ab_log.json", AB_LOG_FILE)


def backup_ab_log():
    return _backup_file(AB_LOG_FILE, "title_ab_log.json", "Auto: update AB title log")


# ── posted_topics.txt (NEW — previously never backed up) ──────────────────

def restore_posted_topics():
    print("Restoring posted topics from GitHub...")
    return _restore_file("posted_topics.txt", POSTED_FILE)


def backup_posted_topics():
    return _backup_file(POSTED_FILE, "posted_topics.txt", "Auto: update posted topics")


# ── aicarryon.db (NEW — direct DB backup, belt-and-suspenders alongside
#    the JSON-based migrate_from_json path) ────────────────────────────────

def restore_sqlite_db(branch=None, local_path=None):
    print(f"Restoring aicarryon.db from GitHub branch {branch or BRANCH} (if present)...")
    return _restore_file("aicarryon.db", local_path or DB_FILE, branch=branch)


def backup_sqlite_db():
    return _backup_file(DB_FILE, "aicarryon.db", f"Backup aicarryon.db — {datetime.now(timezone.utc).isoformat()}")


# ── Convenience: restore/backup everything at once ─────────────────────────

def restore_all():
    restore_view_history()
    restore_ab_log()
    restore_posted_topics()


def backup_all():
    backup_view_history()
    backup_ab_log()
    backup_posted_topics()
