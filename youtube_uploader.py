from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILE = Path(os.environ.get("YOUTUBE_TOKEN_FILE", "youtube_token.json"))
CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
PRIVACY = os.environ.get("YOUTUBE_UPLOAD_PRIVACY", "private").strip().lower()


def authorize() -> Credentials:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set")
    creds = None
    if REFRESH_TOKEN:
        creds = Credentials(
            token=None,
            refresh_token=REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=SCOPES,
        )
    if TOKEN_FILE.is_file():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        client_config = {
            "installed": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        print("Open this URL in a browser, authorize your YouTube account, and paste the code if requested:")
        creds = flow.run_console()
        TOKEN_FILE.write_text(creds.to_json())
        try:
            TOKEN_FILE.chmod(0o600)
        except OSError:
            pass
    return creds


def upload_video(path: str | Path, title: str, description: str = "", tags: list[str] | None = None, source_url: str = "") -> dict[str, Any]:
    privacy = PRIVACY if PRIVACY in {"private", "unlisted", "public"} else "private"
    description = description.strip()
    if source_url and source_url not in description:
        description = f"{description}\n\nOriginal source: {source_url}".strip()
    youtube = build("youtube", "v3", credentials=authorize())
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags or [],
            "categoryId": "10",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response


if __name__ == "__main__":
    print("OAuth uploader ready. Set YOUTUBE_CLIENT_SECRET_FILE and run the authorization from the listener deployment.")
