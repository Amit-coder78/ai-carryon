# agents_horror/upload_agent.py
"""YouTube upload for the Horror channel. Same pattern as agents_cricket/upload_agent.py with the
Horror channel's own token (HORROR_YOUTUBE_TOKEN_B64 or horror_token.pickle)."""
import base64
import os
import pickle

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/youtube.force-ssl"]


def has_credentials():
    return bool(os.getenv("HORROR_YOUTUBE_TOKEN_B64")) or os.path.exists("horror_token.pickle")


def authenticate_youtube():
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = None
    token_b64 = os.getenv("HORROR_YOUTUBE_TOKEN_B64")
    if token_b64:
        creds = pickle.loads(base64.b64decode(token_b64))
    elif os.path.exists("horror_token.pickle"):
        with open("horror_token.pickle", "rb") as f:
            creds = pickle.load(f)
    if not creds:
        raise RuntimeError("No Horror YouTube token. Create one with the Horror channel's Google account "
                           "(same method as generate_cricket_token.py), base64 it into HORROR_YOUTUBE_TOKEN_B64.")
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_video(video_path, title, description, hashtags, tags=None):
    from googleapiclient.http import MediaFileUpload
    youtube = authenticate_youtube()
    hashtag_str = " ".join(hashtags) if isinstance(hashtags, list) else hashtags
    body = {
        "snippet": {"title": title[:100], "description": f"{description}\n\n{hashtag_str}"[:4900],
                    "tags": (tags or [])[:15], "categoryId": "24"},  # Entertainment
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=1024 * 1024 * 5)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")
    vid = response["id"]
    return vid, f"https://www.youtube.com/watch?v={vid}"
