from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
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
    if creds and creds.refresh_token and (creds.expired or not creds.token):
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise RuntimeError(f"YouTube refresh-token exchange failed: {exc}") from exc
    if not creds or not creds.valid:
        raise RuntimeError(
            "YOUTUBE_REFRESH_TOKEN is missing or invalid. Complete the browser OAuth setup again "
            "and add the resulting refresh token to the runtime environment."
        )
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
