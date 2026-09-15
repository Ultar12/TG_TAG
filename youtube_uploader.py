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
CLIENT_FILE = Path(os.environ.get("YOUTUBE_CLIENT_SECRET_FILE", "youtube_client_secret.json"))
TOKEN_FILE = Path(os.environ.get("YOUTUBE_TOKEN_FILE", "youtube_token.json"))
PRIVACY = os.environ.get("YOUTUBE_UPLOAD_PRIVACY", "private").strip().lower()


def authorize() -> Credentials:
    if not CLIENT_FILE.is_file():
        raise FileNotFoundError(f"OAuth client file not found: {CLIENT_FILE}")
    creds = None
    if TOKEN_FILE.is_file():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
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
