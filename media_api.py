from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests
import tornado.web
from tornado.httpserver import HTTPServer
import yt_dlp
from telegram import Update
from telegram.ext import Application

logger = logging.getLogger(__name__)
MAX_API_FILE_BYTES = 49 * 1024 * 1024
MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class MediaAPIError(RuntimeError):
    pass


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MediaAPIError("A valid HTTP or HTTPS URL is required.")
    return url


def _safe_query(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:300]


def _file_has_audio(path: str) -> bool:
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0", path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return probe.returncode == 0 and bool(probe.stdout.strip())
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def _downloaded_candidates(directory: str) -> list[str]:
    return sorted(
        [
            str(path) for path in Path(directory).iterdir()
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        ],
        key=lambda path: os.path.getsize(path),
        reverse=True,
    )


def _copy_valid_download(directory: str, output_path: str, require_audio: bool) -> str:
    candidates = _downloaded_candidates(directory)
    if not candidates:
        raise MediaAPIError(
            "The downloader returned no file. The URL may be private, blocked, or require cookies."
        )

    selected = None
    for candidate in candidates:
        if not require_audio or _file_has_audio(candidate):
            selected = candidate
            break
    if not selected:
        raise MediaAPIError(
            "The downloaded video has no audio stream. Check that ffmpeg is installed and retry."
        )

    if os.path.getsize(selected) > MAX_API_FILE_BYTES:
        raise MediaAPIError("The downloaded file is larger than Telegram's 49 MB API limit.")
    shutil.copyfile(selected, output_path)
    return output_path


def _base_ytdl_options(common_options: Mapping[str, Any]) -> dict[str, Any]:
    options = dict(common_options)
    options.setdefault("quiet", True)
    options.setdefault("no_warnings", False)
    options.setdefault("noplaylist", True)
    return options


def _download_video_sync(
    source_url: str,
    common_options: Mapping[str, Any],
    require_audio: bool = True,
) -> str:
    with tempfile.TemporaryDirectory(prefix="tg_tag_api_video_") as directory:
        output_path = os.path.join(directory, "video.mp4")
        options = _base_ytdl_options(common_options)
        options.update(
            {
                "outtmpl": os.path.join(directory, "%(title).120B.%(ext)s"),
                "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                "merge_output_format": "mp4",
                "recodevideo": "mp4",
            }
        )
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([source_url])
        _copy_valid_download(directory, output_path, require_audio=require_audio)
        return Path(output_path).read_bytes()


def _download_audio_sync(source_url: str, common_options: Mapping[str, Any]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="tg_tag_api_audio_") as directory:
        output_template = os.path.join(directory, "%(title).120B.%(ext)s")
        options = _base_ytdl_options(common_options)
        options.update(
            {
                "outtmpl": output_template,
                "format": "bestaudio/best",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
                ],
            }
        )
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([source_url])
        candidates = _downloaded_candidates(directory)
        mp3_candidates = [path for path in candidates if path.lower().endswith(".mp3")]
        selected = (mp3_candidates or candidates[:1])
        if not selected:
            raise MediaAPIError("YouTube returned no usable audio file.")
        if os.path.getsize(selected[0]) > MAX_API_FILE_BYTES:
            raise MediaAPIError("The downloaded audio is larger than Telegram's 49 MB API limit.")
        return Path(selected[0]).read_bytes()


def _search_youtube_sync(query: str, common_options: Mapping[str, Any]) -> dict[str, str]:
    options = _base_ytdl_options(common_options)
    options.update({"default_search": "ytsearch1", "extract_flat": True})
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(f"ytsearch1:{query}", download=False)
    entries = [entry for entry in (info or {}).get("entries", []) if entry and entry.get("id")]
    if not entries:
        raise MediaAPIError("YouTube returned no music or video result.")
    entry = entries[0]
    video_id = entry["id"]
    return {
        "id": video_id,
        "title": str(entry.get("title") or query),
        "artist": str(entry.get("uploader") or entry.get("channel") or ""),
        "url": str(entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"),
    }


def _download_tikwm_sync(url: str) -> tuple[str, Any] | None:
    response = requests.get(
        "https://www.tikwm.com/api/",
        params={"url": url, "hd": "1"},
        headers={"User-Agent": MEDIA_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    data = (response.json() or {}).get("data") or {}
    caption = str(data.get("title") or "")
    images = data.get("images") or []
    if isinstance(images, list) and images:
        return "json", {"type": "images", "urls": images, "caption": caption}

    media_url = data.get("hdplay") or data.get("play")
    if not media_url:
        return None
    media_response = requests.get(
        media_url,
        headers={"User-Agent": MEDIA_USER_AGENT, "Referer": "https://www.tiktok.com/"},
        timeout=120,
    )
    media_response.raise_for_status()
    if len(media_response.content) > MAX_API_FILE_BYTES:
        raise MediaAPIError("The TikTok video is larger than Telegram's 49 MB API limit.")
    return "video", (media_response.content, caption)


class _BaseHandler(tornado.web.RequestHandler):
    def initialize(self, common_options: Mapping[str, Any]) -> None:
        self.common_options = common_options

    def _json_body(self) -> dict[str, Any]:
        if not self.request.body:
            return {}
        try:
            value = json.loads(self.request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise tornado.web.HTTPError(400, reason="Request body must be valid JSON.") from exc
        return value if isinstance(value, dict) else {}

    def _write_media(self, content: bytes, filename: str, content_type: str) -> None:
        if len(content) > MAX_API_FILE_BYTES:
            raise tornado.web.HTTPError(413, reason="Media exceeds the 49 MB API limit.")
        self.set_header("Content-Type", content_type)
        self.set_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.set_header("Content-Length", str(len(content)))
        self.write(content)


class HealthHandler(tornado.web.RequestHandler):
    def get(self) -> None:
        self.set_header("Content-Type", "application/json")
        self.write({"status": "ok", "service": "TG_TAG"})


class DownloadHandler(_BaseHandler):
    async def get(self) -> None:
        await self._handle(self.get_query_argument("url", default=""))

    async def post(self) -> None:
        body = self._json_body()
        await self._handle(body.get("url") or self.get_body_argument("url", default=""))

    async def _handle(self, raw_url: Any) -> None:
        url = _safe_url(raw_url)
        host = urlparse(url).netloc.lower()
        try:
            if "tiktok.com" in host:
                tikwm_result = await asyncio.to_thread(_download_tikwm_sync, url)
                if tikwm_result:
                    result_type, payload = tikwm_result
                    if result_type == "json":
                        self.set_header("Content-Type", "application/json")
                        self.write(payload)
                        return
                    content, caption = payload
                    if caption:
                        self.set_header("X-Media-Caption", caption.encode("utf-8").hex())
                    self._write_media(content, "tiktok-video.mp4", "video/mp4")
                    return

            content = await asyncio.to_thread(
                _download_video_sync, url, self.common_options, "youtube.com" in host or "youtu.be" in host
            )
            self._write_media(content, "downloaded-video.mp4", "video/mp4")
        except tornado.web.HTTPError:
            raise
        except Exception as exc:
            logger.exception("/api/download failed for %s", url)
            self.set_status(502)
            self.write({"error": str(exc)})


class PlayHandler(_BaseHandler):
    async def post(self) -> None:
        await self._handle(self._json_body())

    async def get(self) -> None:
        await self._handle({"query": self.get_query_argument("query", default="")})

    async def _handle(self, body: Mapping[str, Any]) -> None:
        query = _safe_query(body.get("query"))
        if not query:
            raise tornado.web.HTTPError(400, reason="Missing query.")
        try:
            track = await asyncio.to_thread(_search_youtube_sync, query, self.common_options)
            audio = await asyncio.to_thread(_download_audio_sync, track["url"], self.common_options)
            self.set_header("X-Track-Title", track["title"])
            self.set_header("X-Track-Artist", track["artist"])
            self.set_header("X-Track-Source", "youtube")
            self._write_media(audio, "audio.mp3", "audio/mpeg")
        except tornado.web.HTTPError:
            raise
        except Exception as exc:
            logger.exception("/api/play failed for %s", query)
            self.set_status(502)
            self.write({"error": str(exc)})


class TelegramWebhookHandler(tornado.web.RequestHandler):
    def initialize(self, bot: Any, update_queue: Any, secret_token: str | None = None) -> None:
        self.bot = bot
        self.update_queue = update_queue
        self.secret_token = secret_token

    async def post(self) -> None:
        if self.request.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise tornado.web.HTTPError(403, reason="Telegram webhook requests must be JSON.")
        if self.secret_token and self.request.headers.get("X-Telegram-Bot-Api-Secret-Token") != self.secret_token:
            raise tornado.web.HTTPError(403, reason="Invalid Telegram webhook secret.")
        try:
            update = Update.de_json(json.loads(self.request.body.decode("utf-8")), self.bot)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise tornado.web.HTTPError(400, reason="Invalid Telegram update.") from exc
        if update:
            await self.update_queue.put(update)
        self.set_status(200)
        self.finish()


async def _run_combined_webhook(
    application: Application,
    bot_token: str,
    webhook_url: str,
    port: int,
    common_options: Mapping[str, Any],
    webhook_secret: str | None,
) -> None:
    await application.initialize()
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=webhook_secret,
        drop_pending_updates=False,
    )
    await application.start()

    webhook_path = re.escape(bot_token)
    tornado_app = tornado.web.Application(
        [
            (rf"/{webhook_path}/?", TelegramWebhookHandler, {"bot": application.bot, "update_queue": application.update_queue, "secret_token": webhook_secret}),
            (r"/api/download/?", DownloadHandler, {"common_options": common_options}),
            (r"/api/play-hook/?", PlayHandler, {"common_options": common_options}),
            (r"/api/play/?", PlayHandler, {"common_options": common_options}),
            (r"/", HealthHandler),
            (r"/health/?", HealthHandler),
        ]
    )
    server = HTTPServer(tornado_app)
    server.listen(port, address="0.0.0.0")
    logger.info("Combined Telegram webhook and media API listening on 0.0.0.0:%s", port)

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(stop_signal, stopped.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stopped.wait()
    finally:
        server.stop()
        await application.stop()
        await application.shutdown()


def run_combined_webhook(
    application: Application,
    bot_token: str,
    webhook_url: str,
    port: int,
    common_options: Mapping[str, Any],
    webhook_secret: str | None = None,
) -> None:
    """Run Telegram’s webhook and the scraper-compatible media API on one Heroku port."""
    asyncio.run(
        _run_combined_webhook(
            application,
            bot_token,
            webhook_url,
            port,
            common_options,
            webhook_secret,
        )
    )
