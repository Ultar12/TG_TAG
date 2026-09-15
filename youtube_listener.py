import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import requests
import yt_dlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s - youtube-listener - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
CHAT_ID = ADMIN_ID
CHANNEL = os.environ.get("YOUTUBE_LISTENER_CHANNEL", "@helenefischer").strip()
INTERVAL = max(30, int(os.environ.get("YOUTUBE_LISTENER_INTERVAL", "3600")))
STATE_FILE = Path(os.environ.get("YOUTUBE_LISTENER_STATE_FILE", "/tmp/youtube-listener-state.json"))
COOKIES = os.environ.get("YTDL_COOKIES_FILE", "").strip()
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"


def bot_api(method, payload, files=None):
    response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=payload, files=files, timeout=120)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Telegram API request failed"))
    return body


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"videos": []}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def ytdlp_options():
    options = {
        "quiet": True, "no_warnings": True, "extract_flat": True,
        "skip_download": True, "noplaylist": False, "playlistend": 25,
        "http_headers": {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        "js_runtimes": {"node": {}},
    }
    if COOKIES and os.path.isfile(COOKIES):
        options["cookiefile"] = COOKIES
    return options


def list_videos():
    with yt_dlp.YoutubeDL(ytdlp_options()) as ydl:
        info = ydl.extract_info(f"{CHANNEL.rstrip('/')}/videos", download=False)
    return [entry for entry in (info or {}).get("entries", []) if entry and entry.get("id")]


def download_video(video):
    directory = tempfile.mkdtemp(prefix="tg-tag-youtube-listener-")
    options = {
        "quiet": True, "no_warnings": True,
        "outtmpl": os.path.join(directory, "video.%(ext)s"),
        "format": "best[height<=1080][ext=mp4]/best[height<=1080]/best",
        "merge_output_format": "mp4", "http_headers": {"User-Agent": USER_AGENT},
        "js_runtimes": {"node": {}},
    }
    if COOKIES and os.path.isfile(COOKIES):
        options["cookiefile"] = COOKIES
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([video.get("webpage_url") or f"https://www.youtube.com/watch?v={video['id']}"])
    files = [p for p in Path(directory).iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("yt-dlp produced no video file")
    return directory, files[0]


def forward_video(video):
    directory, path = download_video(video)
    try:
        title = str(video.get("title") or "New YouTube video")
        url = video.get("webpage_url") or f"https://www.youtube.com/watch?v={video['id']}"
        caption = f"New video detected: {title}\n\nSource: {url}"[:1024]
        keyboard = json.dumps({
            "inline_keyboard": [[
                {"text": "Upload", "callback_data": f"yt_approve:{video['id']}"},
                {"text": "Reject", "callback_data": f"yt_reject:{video['id']}"},
            ]]
        })
        with path.open("rb") as media:
            bot_api(
                "sendVideo",
                {"chat_id": CHAT_ID, "caption": caption, "supports_streaming": "true", "reply_markup": keyboard},
                {"video": media},
            )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def poll_once(state):
    videos = list_videos()
    known = set(state.get("videos", []))
    current_ids = [str(v["id"]) for v in videos]
    if not state.get("videos"):
        state["videos"] = current_ids[:25]
        save_state(state)
        logger.info("Initialized with %s existing videos; no backlog sent.", len(current_ids))
        return
    for video in reversed(videos):
        video_id = str(video["id"])
        if video_id in known:
            continue
        logger.info("New YouTube video detected: %s", video_id)
        forward_video(video)
        state.setdefault("videos", []).append(video_id)
    state["videos"] = (state.get("videos", []) + current_ids)[-50:]
    save_state(state)


def main():
    if not BOT_TOKEN or not ADMIN_ID:
        raise SystemExit("BOT_TOKEN and ADMIN_ID are required")
    logger.info("Monitoring %s every %ss", CHANNEL, INTERVAL)
    state = load_state()
    while True:
        try:
            poll_once(state)
        except Exception:
            logger.exception("YouTube listener poll failed")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
