"""Thin, testable wrapper over the YouTube Data API v3 (official API only; no scraping)."""
import json
import socket

OK, TRANSIENT, PERMANENT, ABORT, AMBIGUOUS = "ok", "transient", "permanent", "abort", "ambiguous"


class YouTubeAPIError(Exception):
    def __init__(self, kind, reason, message=""):
        super().__init__(f"{kind}:{reason} {message}".strip())
        self.kind, self.reason = kind, reason


def classify_error(exc, writing=False):
    """Map an exception to (kind, reason). `writing=True` makes timeouts/5xx AMBIGUOUS:
    the reply may or may not have been posted, so the caller must reconcile."""
    status, reason = None, ""
    resp = getattr(exc, "resp", None)
    if resp is not None:
        try:
            status = int(resp.status)
        except (TypeError, ValueError):
            pass
        try:
            err = json.loads(getattr(exc, "content", b"") or b"{}").get("error", {})
            errors = err.get("errors") or [{}]
            reason = (errors[0].get("reason") or err.get("status") or "").strip()
        except (ValueError, AttributeError, TypeError):
            pass
    if status is None and isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return (AMBIGUOUS if writing else TRANSIENT), "network"
    if status is None:
        text = str(exc).lower()
        if "invalid_grant" in text or "token" in text and "expired" in text or "revoked" in text:
            return ABORT, "auth"
        return TRANSIENT, "unknown"
    if status in (401,) or reason in ("authError", "UNAUTHENTICATED"):
        return ABORT, "auth"
    if status == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
                                    "userRateLimitExceeded"):
        return ABORT, "quota"
    if status == 403 and reason in ("insufficientPermissions", "forbidden", "accessNotConfigured"):
        return ABORT, "scope"
    if status == 403 and reason in ("commentsDisabled", "processingFailure"):
        return PERMANENT, reason
    if status in (400, 403, 404) or reason in ("commentNotFound", "videoNotFound", "parentCommentIdNotFound"):
        return PERMANENT, reason or str(status)
    if status == 429:
        return TRANSIENT, "rate_limit"
    if status >= 500:
        return (AMBIGUOUS if writing else TRANSIENT), f"http_{status}"
    return TRANSIENT, reason or str(status)


def _wrap(exc, writing=False):
    if isinstance(exc, YouTubeAPIError):
        return exc
    kind, reason = classify_error(exc, writing)
    return YouTubeAPIError(kind, reason, str(exc)[:200])


class YouTubeCommentClient:
    """`service` is an authenticated googleapiclient resource for ONE channel."""

    def __init__(self, service):
        self.service = service
        self._own_channel_id = None

    def own_channel_id(self):
        if self._own_channel_id is None:
            try:
                r = self.service.channels().list(part="id", mine=True).execute()
                items = r.get("items") or []
            except Exception as e:
                raise _wrap(e)
            if not items:
                raise YouTubeAPIError(ABORT, "no_channel", "authorised account has no channel")
            self._own_channel_id = items[0]["id"]
        return self._own_channel_id

    def list_threads(self, max_results=50, max_pages=3):
        """Newest-first top-level threads for the authorised channel (bounded)."""
        own = self.own_channel_id()
        out, token = [], None
        for _ in range(max_pages):
            try:
                resp = self.service.commentThreads().list(
                    part="snippet,replies", allThreadsRelatedToChannelId=own, maxResults=min(100, max_results),
                    order="time", textFormat="plainText", pageToken=token).execute()
            except Exception as e:
                raise _wrap(e)
            for it in resp.get("items", []):
                n = self._normalise(it, own)
                if n:
                    out.append(n)
            token = resp.get("nextPageToken")
            if not token or len(out) >= max_results:
                break
        return out[:max_results]

    @staticmethod
    def _normalise(item, own_channel_id):
        top = (item.get("snippet") or {}).get("topLevelComment") or {}
        s = top.get("snippet") or {}
        if not top.get("id"):
            return None
        replies = []
        for r in (item.get("replies") or {}).get("comments", []):
            rs = r.get("snippet") or {}
            replies.append({"id": r.get("id"), "text": rs.get("textDisplay", ""),
                            "author_channel_id": (rs.get("authorChannelId") or {}).get("value", ""),
                            "author_name": rs.get("authorDisplayName", "")})
        return {
            "comment_id": top["id"], "video_id": s.get("videoId") or item["snippet"].get("videoId", ""),
            "text": s.get("textDisplay") or s.get("textOriginal") or "",
            "author_name": s.get("authorDisplayName", ""),
            "author_channel_id": (s.get("authorChannelId") or {}).get("value", ""),
            "published_at": s.get("publishedAt", ""),
            "can_reply": bool(item["snippet"].get("canReply", True)),
            "total_reply_count": int(item["snippet"].get("totalReplyCount", 0)),
            "replies": replies,
            "own_replied": any(r["author_channel_id"] == own_channel_id for r in replies),
            "is_own": (s.get("authorChannelId") or {}).get("value", "") == own_channel_id,
        }

    def get_thread(self, comment_id):
        """Fresh single-thread read (used for the pre-publish eligibility check). None if gone."""
        own = self.own_channel_id()
        try:
            resp = self.service.commentThreads().list(part="snippet,replies", id=comment_id,
                                                      textFormat="plainText").execute()
        except Exception as e:
            err = _wrap(e)
            if err.kind == PERMANENT:
                return None
            raise err
        items = resp.get("items") or []
        return self._normalise(items[0], own) if items else None

    def get_video(self, video_id):
        try:
            resp = self.service.videos().list(part="snippet", id=video_id).execute()
        except Exception as e:
            raise _wrap(e)
        items = resp.get("items") or []
        if not items:
            return None
        sn = items[0].get("snippet", {})
        return {"title": sn.get("title", ""), "description": sn.get("description", "")}

    def insert_reply(self, parent_id, text):
        try:
            resp = self.service.comments().insert(
                part="snippet", body={"snippet": {"parentId": parent_id, "textOriginal": text}}).execute()
        except Exception as e:
            raise _wrap(e, writing=True)
        return resp.get("id")

    def find_reply(self, parent_id, text):
        """Reconciliation: did our channel already post `text` under this thread?"""
        from .deduplication import normalize
        own = self.own_channel_id()
        try:
            resp = self.service.comments().list(part="snippet", parentId=parent_id, maxResults=100,
                                                textFormat="plainText").execute()
        except Exception as e:
            raise _wrap(e)
        want = normalize(text)
        for c in resp.get("items", []):
            s = c.get("snippet", {})
            if (s.get("authorChannelId") or {}).get("value") == own and \
                    normalize(s.get("textDisplay") or s.get("textOriginal") or "") == want:
                return c.get("id")
        return None
