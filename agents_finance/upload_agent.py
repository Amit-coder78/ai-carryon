# agents_finance/upload_agent.py
"""Finance channel YouTube uploader. Same pattern as agents_bhakti/upload_agent.py:
token JSON comes from FINANCE_TOKEN_JSON (GitHub secret) or a local finance_token.json
written by generate_finance_token.py. The description from build_metadata already
carries sources, the risk disclaimer and hashtags, so it is sent unchanged."""
import json
import os
import time

import google.oauth2.credentials
import googleapiclient.discovery
import googleapiclient.http

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
LOCAL_TOKEN_FILES = ("finance_token.json", "token_finance.json")


def _token_json():
    token_json = os.getenv("FINANCE_TOKEN_JSON")
    if token_json:
        return token_json
    for path in LOCAL_TOKEN_FILES:
        if os.path.exists(path):
            with open(path) as f:
                return f.read()
    raise ValueError("No Finance YouTube token found: set FINANCE_TOKEN_JSON or run generate_finance_token.py")


def get_youtube_client():
    data = json.loads(_token_json())
    credentials = google.oauth2.credentials.Credentials(
        token=data.get("token"),
        refresh_token=data["refresh_token"],
        token_uri=data["token_uri"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data.get("scopes", SCOPES),
    )
    return googleapiclient.discovery.build("youtube", "v3", credentials=credentials, cache_discovery=False)


def upload_video(video_path, title, description, hashtags, thumbnail_path=None):
    youtube = get_youtube_client()
    if isinstance(hashtags, str):
        hashtags = hashtags.replace(",", " ").split()
    tags = [h.lstrip("#") for h in (hashtags or []) if h.strip("#")]

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:30],
            "categoryId": "27",  # Education
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": os.getenv("FINANCE_PRIVACY", "private"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = googleapiclient.http.MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        for retry in range(3):
            try:
                status, response = request.next_chunk()
                break
            except Exception as err:
                if retry == 2:
                    raise
                print(f"Chunk upload retry {retry + 1}: {err}")
                time.sleep(5)
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")

    video_id = response["id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=googleapiclient.http.MediaFileUpload(thumbnail_path)).execute()
        except Exception as e:
            print(f"Thumbnail upload failed: {e}")
    print(f"Uploaded! {video_url} (privacy={body['status']['privacyStatus']})")
    return video_id, video_url
