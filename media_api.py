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
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import requests
import tornado.web
from tornado.httpserver import HTTPServer
import yt_dlp
from telegram import Update
from telegram.ext import Application

logger = logging.getLogger(__name__)
MAX_API_FILE_BYTES = 2 * 1024 * 1024 * 1024
PLAY_JOB_TTL_SECONDS = 1800
PLAY_JOB_MAX_ACTIVE = 2
PLAY_JOBS: dict[str, dict[str, Any]] = {}
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


PINTEREST_HOSTS = {"pinterest.com", "pin.it"}
PINTEREST_PINIMG_RE = re.compile(
    r"https?://(?:i|v1)\.pinimg\.com/[^\s\"'<>),;}\]]+",
    re.IGNORECASE,
)
PINTEREST_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:video|og:video:url|twitter:player:stream)[\"'][^>]+content=[\"']([^\"']+)",
    re.IGNORECASE,
)
PINTEREST_IMAGE_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)[\"'][^>]+content=[\"']([^\"']+)",
    re.IGNORECASE,
)


def _is_pinterest_url(value: str) -> bool:
    try:
        host = urlparse(value).netloc.lower().split(":", 1)[0]
        return host in PINTEREST_HOSTS or host.endswith(".pinterest.com")
    except Exception:
        return False


def _clean_pinterest_url(value: str, base_url: str) -> str:
    cleaned = (
        str(value or "")
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
        .strip()
        .rstrip("),.;}]")
    )
    if not cleaned:
        return ""
    try:
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"}:
            return ""
        return cleaned
    except Exception:
        return ""


def _normalize_pinterest_image_url(value: str) -> str:
    normalized = _clean_pinterest_url(value, "https://www.pinterest.com/")
    if not normalized or "pinimg.com" not in normalized:
        return normalized
    return re.sub(r"/(?:\d+x\d*|originals)/", "/originals/", normalized, flags=re.IGNORECASE)


def _pinterest_media_candidates(html: str, page_url: str) -> tuple[list[str], list[str]]:
    video_urls = []
    image_urls = []

    for value in PINTEREST_META_RE.findall(html):
        cleaned = _clean_pinterest_url(value, page_url)
        if cleaned:
            video_urls.append(cleaned)

    for value in PINTEREST_IMAGE_META_RE.findall(html):
        cleaned = _normalize_pinterest_image_url(value)
        if cleaned:
            image_urls.append(cleaned)

    searchable_html = (
        html
        .replace("\\u002F", "/")
        .replace("\\/", "/")
    )
    for match in PINTEREST_PINIMG_RE.findall(searchable_html):
        cleaned = _clean_pinterest_url(match, page_url)
        if not cleaned:
            continue
        if re.search(r"(?:\.mp4|\.m3u8)(?:[?#]|$)|/videos/", cleaned, re.IGNORECASE):
            video_urls.append(cleaned)
        else:
            image_urls.append(_normalize_pinterest_image_url(cleaned))

    def unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    return unique(video_urls), unique(image_urls)


def _download_pinterest_sync(source_url: str) -> tuple[str, Any]:
    headers = {
        "User-Agent": MEDIA_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pinterest.com/",
    }
    response = requests.get(
        source_url,
        headers=headers,
        timeout=45,
        allow_redirects=True,
    )
    response.raise_for_status()
    page_url = response.url or source_url
    video_urls, image_urls = _pinterest_media_candidates(response.text, page_url)

    preferred_video = next(
        (
            value for value in video_urls
            if re.search(r"(?:\.mp4|\.m3u8)(?:[?#]|$)|/videos/", value, re.IGNORECASE)
        ),
        None,
    )
    if preferred_video:
        if re.search(r"\.m3u8(?:[?#]|$)", preferred_video, re.IGNORECASE):
            directory = tempfile.mkdtemp(prefix="tg_tag_pinterest_video_")
            try:
                options = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "outtmpl": os.path.join(directory, "%(title).120B.%(ext)s"),
                    "format": "best",
                    "merge_output_format": "mp4",
                }
                with yt_dlp.YoutubeDL(options) as downloader:
                    downloader.download([preferred_video])
                source_path = os.path.join(directory, "pinterest-video.source")
                raw_output_path = os.path.join(directory, "pinterest-video.raw")
                output_path = os.path.join(directory, "pinterest-video.mp4")
                _copy_pinterest_source(directory, raw_output_path)
                _normalize_pinterest_video(raw_output_path, output_path)
                return "video", Path(output_path).read_bytes()
            finally:
                shutil.rmtree(directory, ignore_errors=True)

        media_response = requests.get(
            preferred_video,
            headers=headers,
            timeout=120,
            stream=True,
        )
        media_response.raise_for_status()
        content = media_response.content
        if not content or len(content) < 1024:
            raise MediaAPIError("Pinterest returned an empty video file.")
        if len(content) > MAX_API_FILE_BYTES:
            raise MediaAPIError("The Pinterest video is larger than the supported 2 GB limit.")

        with tempfile.TemporaryDirectory(prefix="tg_tag_pinterest_video_") as directory:
            source_path = os.path.join(directory, "pinterest-video.source")
            output_path = os.path.join(directory, "pinterest-video.mp4")
            Path(source_path).write_bytes(content)
            _normalize_pinterest_video(source_path, output_path)
            return "video", Path(output_path).read_bytes()

    usable_images = [
        value for value in image_urls
        if not re.search(r"/(?:75x75|236x|474x|564x|60x60)/", value, re.IGNORECASE)
    ]
    if usable_images:
        return "images", usable_images[:1]

    raise MediaAPIError(
        "Pinterest returned no public image or video URL. The pin may be private, deleted, or login-gated."
    )


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


def _file_is_mp4(path: str) -> bool:
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=format_name",
                "-of", "default=nw=1:nk=1", path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        format_names = {
            item.strip()
            for item in probe.stdout.split(",")
            if item.strip()
        }
        return probe.returncode == 0 and "mp4" in format_names
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def _run_ffmpeg(args: list[str], timeout: int = 600) -> None:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaAPIError("ffmpeg is not installed on the TG_TAG server.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaAPIError("Pinterest video conversion timed out on the TG_TAG server.") from exc
    except OSError as exc:
        raise MediaAPIError(f"ffmpeg could not start: {exc}") from exc

    if result.returncode == 0:
        return

    diagnostics = " ".join(
        line.strip()
        for line in result.stderr.splitlines()
        if line.strip()
    )[-1200:]
    raise MediaAPIError(
        f"Pinterest video conversion failed with code {result.returncode}: "
        f"{diagnostics or 'no diagnostic output'}"
    )


def _normalize_pinterest_video(input_path: str, output_path: str) -> str:
    _run_ffmpeg(
        [
            "-fflags", "+genpts",
            "-i", input_path,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-map_metadata", "-1",
            "-sn",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-profile:v", "main",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ],
    )
    if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1024:
        raise MediaAPIError("Pinterest conversion produced an empty MP4 file.")
    if not _file_is_mp4(output_path):
        raise MediaAPIError("Pinterest conversion did not produce a valid MP4 file.")
    return output_path


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
        if not _file_is_mp4(candidate):
            continue
        if not require_audio or _file_has_audio(candidate):
            selected = candidate
            break
    if not selected:
        raise MediaAPIError(
            "The downloader did not return an MP4 video with the required audio stream."
        )

    if os.path.getsize(selected) > MAX_API_FILE_BYTES:
        raise MediaAPIError("The downloaded file is larger than the supported 2 GB limit.")
    shutil.copyfile(selected, output_path)
    return output_path


def _copy_pinterest_source(directory: str, output_path: str) -> str:
    candidates = _downloaded_candidates(directory)
    if not candidates:
        raise MediaAPIError("Pinterest video download returned no source file.")

    selected = candidates[0]
    if os.path.getsize(selected) > MAX_API_FILE_BYTES:
        raise MediaAPIError("The Pinterest video is larger than the supported 2 GB limit.")
    shutil.copyfile(selected, output_path)
    return output_path


def _base_ytdl_options(common_options: Mapping[str, Any]) -> dict[str, Any]:
    options = dict(common_options)
    options.setdefault("quiet", True)
    options.setdefault("no_warnings", False)
    options.setdefault("noplaylist", True)
    return options


def _normalize_quality_height(value: Any, default: int = 1080) -> int:
    value_text = str(value or "").strip().lower()
    aliases = {
        "4k": 2160,
        "2160p": 2160,
        "2k": 1440,
        "1440p": 1440,
        "hd": 1080,
        "1080p": 1080,
        "720p": 720,
        "720": 720,
    }
    if value_text in aliases:
        return aliases[value_text]
    try:
        height = int(value_text)
    except (TypeError, ValueError):
        return default
    return max(144, min(height, 1080))


def _download_video_file_sync(
    source_url: str,
    common_options: Mapping[str, Any],
    require_audio: bool = True,
    max_height: int = 1080,
) -> tuple[str, str]:
    directory = tempfile.mkdtemp(prefix="tg_tag_api_video_")
    try:
        raw_path = os.path.join(directory, "raw.media")
        options = _base_ytdl_options(common_options)
        options.update(
            {
                "outtmpl": os.path.join(directory, "%(title).120B.%(ext)s"),
                "format": f"best[ext=mp4][height<={max_height}]/bestvideo[ext=mp4][height<={max_height}]+bestaudio[ext=m4a]/best[height<={max_height}][ext=mp4]",
                "merge_output_format": "mp4",
            }
        )
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([source_url])
        _copy_valid_download(directory, raw_path, require_audio=require_audio)
        if os.path.getsize(raw_path) > MAX_API_FILE_BYTES:
            raise MediaAPIError("The downloaded video is larger than the supported 2 GB limit.")
        return raw_path, directory
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _download_audio_sync(source_url: str, common_options: Mapping[str, Any]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="tg_tag_api_audio_") as directory:
        output_template = os.path.join(directory, "%(title).120B.%(ext)s")
        options = _base_ytdl_options(common_options)
        options.update(
            {
                "outtmpl": output_template,
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}
                ],
            }
        )
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([source_url])
        candidates = _downloaded_candidates(directory)
        mp3_candidates = [path for path in candidates if path.lower().endswith(".mp3")]
        if not mp3_candidates:
            raise MediaAPIError("YouTube audio conversion did not produce a valid MP3 file; ffmpeg may be missing.")
        selected = mp3_candidates
        if os.path.getsize(selected[0]) > MAX_API_FILE_BYTES:
            raise MediaAPIError("The downloaded audio is larger than the supported 2 GB limit.")
        return Path(selected[0]).read_bytes()


def _cleanup_play_jobs() -> None:
    now = time.time()
    expired = []
    for job_id, job in PLAY_JOBS.items():
        if now - float(job.get("updated_at", now)) <= PLAY_JOB_TTL_SECONDS:
            continue
        temp_dir = job.get("temp_dir")
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        expired.append(job_id)
    for job_id in expired:
        PLAY_JOBS.pop(job_id, None)


async def _run_play_job(
    job_id: str,
    query: str,
    mode: str,
    common_options: Mapping[str, Any],
) -> None:
    job = PLAY_JOBS.get(job_id)
    if not job:
        return
    try:
        track = await asyncio.to_thread(_search_youtube_sync, query, common_options)
        job.update({
            "state": "processing",
            "title": track["title"],
            "artist": track["artist"],
            "updated_at": time.time(),
        })
        if mode == "video":
            media_path, temp_dir = await asyncio.to_thread(
                _download_video_file_sync,
                track["url"],
                common_options,
                True,
                1080,
            )
            filename = "video.mp4"
            content_type = "video/mp4"
        else:
            media = await asyncio.to_thread(
                _download_audio_sync,
                track["url"],
                common_options,
            )
            temp_dir = tempfile.mkdtemp(prefix="tg_tag_api_play_job_")
            media_path = os.path.join(temp_dir, "audio.mp3")
            Path(media_path).write_bytes(media)
            filename = "audio.mp3"
            content_type = "audio/mpeg"
        job.update({
            "state": "ready",
            "path": media_path,
            "temp_dir": temp_dir,
            "filename": filename,
            "content_type": content_type,
            "size": os.path.getsize(media_path),
            "updated_at": time.time(),
        })
    except Exception as exc:
        logger.exception("Play job %s failed", job_id)
        job.update({
            "state": "failed",
            "error": str(exc),
            "updated_at": time.time(),
        })


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
        raise MediaAPIError("The TikTok video is larger than the supported 2 GB limit.")
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
            raise tornado.web.HTTPError(413, reason="Media exceeds the supported 2 GB limit.")
        self.set_header("Content-Type", content_type)
        self.set_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.set_header("Content-Length", str(len(content)))
        self.write(content)

    async def _stream_file(self, path: str, filename: str, content_type: str) -> None:
        size = os.path.getsize(path)
        if size > MAX_API_FILE_BYTES:
            raise tornado.web.HTTPError(413, reason="Media exceeds the supported 2 GB limit.")
        self.set_header("Content-Type", content_type)
        self.set_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.set_header("Content-Length", str(size))
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                self.write(chunk)
                await self.flush()


class HealthHandler(tornado.web.RequestHandler):
    def get(self) -> None:
        self.set_header("Content-Type", "application/json")
        self.write({"status": "ok", "service": "TG_TAG"})


class DownloadHandler(_BaseHandler):
    async def get(self) -> None:
        await self._handle(self.get_query_argument("url", default=""))

    async def post(self) -> None:
        body = self._json_body()
        await self._handle(
            body.get("url") or self.get_body_argument("url", default=""),
            body.get("quality", ""),
        )

    async def _handle(self, raw_url: Any, requested_quality: Any = "") -> None:
        url = _safe_url(raw_url)
        host = urlparse(url).netloc.lower()
        try:
            if _is_pinterest_url(url):
                pinterest_type, pinterest_payload = await asyncio.to_thread(
                    _download_pinterest_sync,
                    url,
                )
                if pinterest_type == "images":
                    self.set_header("Content-Type", "application/json")
                    self.write(json.dumps({
                        "type": "images",
                        "urls": pinterest_payload,
                    }))
                    return
                self._write_media(
                    pinterest_payload,
                    "pinterest-video.mp4",
                    "video/mp4",
                )
                return

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
                        self.set_header("X-Media-Caption", quote(caption, safe=""))
                    self._write_media(content, "tiktok-video.mp4", "video/mp4")
                    return

            if not requested_quality:
                requested_quality = self.get_query_argument("quality", default="")
            video_path, temp_dir = await asyncio.to_thread(
                _download_video_file_sync,
                url,
                self.common_options,
                "youtube.com" in host or "youtu.be" in host,
                _normalize_quality_height(requested_quality),
            )
            try:
                await self._stream_file(video_path, "downloaded-video.mp4", "video/mp4")
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except tornado.web.HTTPError:
            raise
        except Exception as exc:
            logger.exception("/api/download failed for %s", url)
            self.set_status(502)
            self.write({"error": str(exc)})


class PlayJobCreateHandler(_BaseHandler):
    async def post(self) -> None:
        body = self._json_body()
        query = _safe_query(body.get("query"))
        if not query:
            raise tornado.web.HTTPError(400, reason="Missing query.")

        mode_value = str(body.get("mode") or "audio").strip().lower()
        mode = "video" if mode_value in {"video", "vla", "mp4"} else "audio"
        _cleanup_play_jobs()
        active_jobs = sum(
            1 for item in PLAY_JOBS.values()
            if item.get("state") in {"queued", "processing"}
        )
        if active_jobs >= PLAY_JOB_MAX_ACTIVE:
            self.set_status(429)
            self.write({"error": "The media server is busy. Try again shortly."})
            return

        job_id = uuid.uuid4().hex
        PLAY_JOBS[job_id] = {
            "state": "queued",
            "query": query,
            "mode": mode,
            "updated_at": time.time(),
        }
        asyncio.create_task(
            _run_play_job(job_id, query, mode, self.common_options)
        )
        self.set_status(202)
        self.set_header("Content-Type", "application/json")
        self.write({
            "job_id": job_id,
            "status_url": f"/api/play/{job_id}",
            "result_url": f"/api/play/{job_id}/result",
        })

    async def get(self) -> None:
        await self.post()


class PlayJobStatusHandler(_BaseHandler):
    async def get(self, job_id: str) -> None:
        _cleanup_play_jobs()
        job = PLAY_JOBS.get(job_id)
        if not job:
            raise tornado.web.HTTPError(404, reason="Play job was not found or expired.")
        self.set_header("Content-Type", "application/json")
        self.set_status(200 if job.get("state") in {"ready", "failed"} else 202)
        self.write({
            "job_id": job_id,
            "state": job.get("state"),
            "title": job.get("title", ""),
            "artist": job.get("artist", ""),
            "error": job.get("error", ""),
            "result_url": f"/api/play/{job_id}/result",
        })


class PlayJobResultHandler(_BaseHandler):
    async def get(self, job_id: str) -> None:
        _cleanup_play_jobs()
        job = PLAY_JOBS.get(job_id)
        if not job:
            raise tornado.web.HTTPError(404, reason="Play job was not found or expired.")
        state = job.get("state")
        if state in {"queued", "processing"}:
            self.set_status(202)
            self.set_header("Content-Type", "application/json")
            self.write({"job_id": job_id, "state": state})
            return
        if state == "failed":
            self.set_status(502)
            self.write({"error": job.get("error", "Media job failed.")})
            return
        path = str(job.get("path") or "")
        if not path or not os.path.isfile(path):
            raise tornado.web.HTTPError(410, reason="Play result is no longer available.")
        self.set_header("X-Track-Title", str(job.get("title", "")))
        self.set_header("X-Track-Artist", str(job.get("artist", "")))
        self.set_header("X-Track-Source", "youtube")
        await self._stream_file(
            path,
            str(job.get("filename", "media.bin")),
            str(job.get("content_type", "application/octet-stream")),
        )
        temp_dir = job.get("temp_dir")
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        PLAY_JOBS.pop(job_id, None)


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
            (r"/api/play/([a-f0-9]{32})/result/?", PlayJobResultHandler, {"common_options": common_options}),
            (r"/api/play/([a-f0-9]{32})/?", PlayJobStatusHandler, {"common_options": common_options}),
            (r"/api/play-hook/?", PlayJobCreateHandler, {"common_options": common_options}),
            (r"/api/play/?", PlayJobCreateHandler, {"common_options": common_options}),
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
