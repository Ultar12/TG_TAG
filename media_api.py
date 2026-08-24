from __future__ import annotations

import asyncio
import base64
import io
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
import pymupdf as fitz
from PIL import Image
import yt_dlp
from telegram import Update
from telegram.ext import Application

logger = logging.getLogger(__name__)
MAX_API_FILE_BYTES = 2 * 1024 * 1024 * 1024
PLAY_JOB_TTL_SECONDS = 1800
PLAY_JOB_MAX_ACTIVE = 2
PLAY_JOBS: dict[str, dict[str, Any]] = {}
UAI_HISTORIES: dict[str, list[dict[str, Any]]] = {}
UAI_CHAT_LOCKS: dict[str, asyncio.Lock] = {}
UAI_MAX_FILE_BYTES = 15 * 1024 * 1024
UAI_MAX_TEXT_CHARS = 800_000
UAI_HISTORY_FILE_CHARS = 120_000
UAI_HISTORY_MAX_MESSAGES = 10
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


def _is_facebook_url(source_url: str) -> bool:
    host = urlparse(source_url).netloc.lower().split(':', 1)[0]
    return host in {'facebook.com', 'fb.watch'} or host.endswith('.facebook.com')


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
        if _is_facebook_url(source_url):
            format_selector = f"best[height<={max_height}]/best"
        else:
            format_selector = (
                f"best[ext=mp4][height<={max_height}]/"
                f"bestvideo[ext=mp4][height<={max_height}]+"
                f"bestaudio[ext=m4a]/best[height<={max_height}][ext=mp4]"
            )

        options.update(
            {
                "outtmpl": os.path.join(directory, "%(title).120B.%(ext)s"),
                "format": format_selector,
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


def _uai_clean_text(value: Any, limit: int = UAI_MAX_TEXT_CHARS) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    return text[:limit].strip()


def _uai_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _uai_history_key(value: Any) -> str:
    key = _uai_clean_text(value, 200)
    return key or "default_chat"




def _uai_agent_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in history:
        role = "assistant" if item.get("role") == "assistant" else "user"
        content = item.get("content")
        if isinstance(content, list):
            safe_content = content
        else:
            safe_content = _uai_clean_text(item.get("text") or content, UAI_HISTORY_FILE_CHARS)
        if safe_content:
            messages.append({"role": role, "content": safe_content})
    return messages


def _uai_file_content(upload: dict[str, Any], prompt: str) -> tuple[Any, str]:
    data = upload.get("body") or b""
    filename = _uai_clean_text(upload.get("filename") or "file.bin", 160)
    content_type = str(upload.get("content_type") or "application/octet-stream").lower()
    if len(data) > UAI_MAX_FILE_BYTES:
        raise tornado.web.HTTPError(413, reason="Attached file exceeds the 15 MB limit.")

    if content_type.startswith("image/"):
        try:
            with Image.open(io.BytesIO(data)) as source:
                image = source.convert("RGB")
                image.thumbnail((1200, 1200))
                image.load()
            image_bytes = io.BytesIO()
            image.save(image_bytes, format="JPEG", quality=82, optimize=True)
            encoded = base64.b64encode(image_bytes.getvalue()).decode("ascii")
            text = _uai_clean_text(prompt or f"Analyze the attached image: {filename}.", UAI_HISTORY_FILE_CHARS)
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": encoded,
                    },
                },
                {"type": "text", "text": text},
            ]
            return content, f"[Attached image: {filename}] {text}"
        except Exception as exc:
            raise tornado.web.HTTPError(400, reason="The attached image could not be processed.") from exc

    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        try:
            with fitz.open(stream=data, filetype="pdf") as document:
                extracted = "\n".join(page.get_text() for page in document)
            extracted = _uai_clean_text(extracted, UAI_HISTORY_FILE_CHARS)
            text = (
                f"[Attached PDF: {filename}]\n```\n{extracted}\n```\n\n"
                f"{_uai_clean_text(prompt or 'Analyze this document.', UAI_HISTORY_FILE_CHARS)}"
            )
            return text, text
        except Exception as exc:
            raise tornado.web.HTTPError(400, reason="The attached PDF could not be read.") from exc

    if b"\x00" in data:
        text = (
            f"[Attached binary file: {filename}; the raw contents were not interpreted as text.]\n\n"
            f"{_uai_clean_text(prompt or 'Explain what can be inferred from this file name and type.', UAI_HISTORY_FILE_CHARS)}"
        )
        return text, text

    decoded = data.decode("utf-8", errors="replace")
    decoded = _uai_clean_text(decoded, UAI_HISTORY_FILE_CHARS)
    text = (
        f"[Attached file: {filename}]\n```\n{decoded}\n```\n\n"
        f"{_uai_clean_text(prompt or 'Analyze this file.', UAI_HISTORY_FILE_CHARS)}"
    )
    return text, text


def _uai_extract_reply(data: Any, depth: int = 0) -> str:
    """Extract assistant text from common agent-router response envelopes."""
    if depth > 8:
        return ""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, list):
        parts = [_uai_extract_reply(item, depth + 1) for item in data]
        return "".join(part for part in parts if part).strip()
    if not isinstance(data, dict):
        return ""

    # Direct text fields cover Anthropic content blocks and provider wrappers.
    for key in ("text", "output_text", "completion", "answer", "response", "content", "result"):
        if key in data:
            candidate = _uai_extract_reply(data[key], depth + 1)
            if candidate:
                return candidate

    # OpenAI-compatible choices and streaming delta envelopes.
    for key in ("choices", "output", "data", "message", "delta"):
        if key in data:
            candidate = _uai_extract_reply(data[key], depth + 1)
            if candidate:
                return candidate
    return ""


def _uai_extract_stream_piece(data: Any, depth: int = 0) -> str:
    """Extract one streaming text piece without stripping its whitespace."""
    if depth > 8:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(_uai_extract_stream_piece(item, depth + 1) for item in data)
    if not isinstance(data, dict):
        return ""
    for key in ("text", "output_text", "completion", "content"):
        if key in data:
            piece = _uai_extract_stream_piece(data[key], depth + 1)
            if piece:
                return piece
    for key in ("choices", "output", "data", "message", "delta", "result", "response"):
        if key in data:
            piece = _uai_extract_stream_piece(data[key], depth + 1)
            if piece:
                return piece
    return ""


def _uai_extract_sse_text(raw: str) -> str:
    """Extract text from a text/event-stream response without logging its body."""
    parts = []
    for line in (raw or "").splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except (TypeError, ValueError):
            continue
        text = _uai_extract_stream_piece(chunk)
        if text:
            parts.append(text)
    return "".join(parts).strip()


def _uai_response_data(response: requests.Response) -> Any:
    try:
        data = response.json()
        if data not in ({}, None, ""):
            return data
    except ValueError:
        data = None
    raw = (response.text or "").strip()
    streamed = _uai_extract_sse_text(raw)
    if streamed:
        return {"text": streamed}
    if raw and not raw.startswith("<"):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {"text": raw}
    return data if data not in (None, "") else {}


def _uai_shape(data: Any) -> str:
    if isinstance(data, dict):
        return "object keys=" + ",".join(sorted(str(key) for key in data.keys())[:20])
    if isinstance(data, list):
        return f"array length={len(data)}"
    return type(data).__name__


def _uai_error_text(data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or "agent router error")
        if isinstance(error, str):
            return error
    return "agent router error"


def _uai_agent_endpoint() -> str:
    base_url = (
        os.environ.get("AGENT_ROUTER_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/v1/messages"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/messages"
    return f"{base_url}/v1/messages"


class UAIHandler(tornado.web.RequestHandler):
    """Direct Scraper-compatible AI endpoint using an agent-router messages API."""

    def _authorized(self) -> bool:
        expected = os.environ.get("UAI_API_TOKEN", "").strip()
        return not expected or self.request.headers.get("X-UAI-Token", "") == expected

    def _request_values(self) -> tuple[str, str, bool, dict[str, Any] | None]:
        content_type = self.request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            try:
                body = json.loads(self.request.body.decode("utf-8")) if self.request.body else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise tornado.web.HTTPError(400, reason="Request body must be valid JSON.") from exc
            if not isinstance(body, dict):
                raise tornado.web.HTTPError(400, reason="Request body must be an object.")
            return (
                _uai_clean_text(body.get("prompt"), UAI_HISTORY_FILE_CHARS),
                _uai_history_key(body.get("chatId")),
                _uai_truthy(body.get("resetHistory")),
                None,
            )
        prompt = _uai_clean_text(self.get_body_argument("prompt", default=""), UAI_HISTORY_FILE_CHARS)
        chat_id = _uai_history_key(self.get_body_argument("chatId", default="default_chat"))
        reset_history = _uai_truthy(self.get_body_argument("resetHistory", default="false"))
        uploads = self.request.files.get("file", [])
        return prompt, chat_id, reset_history, (uploads[0] if uploads else None)

    async def post(self) -> None:
        if not self._authorized():
            raise tornado.web.HTTPError(401, reason="Invalid AI API token.")
        prompt, chat_id, reset_history, upload = self._request_values()
        if not prompt and not upload:
            raise tornado.web.HTTPError(400, reason="Provide a prompt or attach a file.")

        agent_url = _uai_agent_endpoint()
        agent_key = (
            os.environ.get("AGENT_ROUTER_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or ""
        ).strip()
        if not agent_url or not agent_key:
            self.set_status(503)
            self.set_header("Content-Type", "application/json")
            self.write({"success": False, "error": "AI agent service is not configured."})
            return

        lock = UAI_CHAT_LOCKS.setdefault(chat_id, asyncio.Lock())
        async with lock:
            try:
                history = [] if reset_history else list(UAI_HISTORIES.get(chat_id, []))
                if upload:
                    model_content, history_text = await asyncio.to_thread(_uai_file_content, upload, prompt)
                else:
                    model_content = prompt
                    history_text = prompt

                messages = _uai_agent_messages(history)
                messages.append({"role": "user", "content": model_content})
                model = (
                    os.environ.get("AGENT_ROUTER_MODEL")
                    or os.environ.get("ANTHROPIC_MODEL")
                    or "claude-opus-5"
                ).strip()
                max_tokens = int(os.environ.get("AGENT_ROUTER_MAX_TOKENS", "8192"))
                payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
                headers = {
                    "Authorization": f"Bearer {agent_key}",
                    "x-api-key": agent_key,
                    "anthropic-version": (
                        os.environ.get("AGENT_ROUTER_ANTHROPIC_VERSION")
                        or os.environ.get("ANTHROPIC_VERSION")
                        or "2023-06-01"
                    ),
                    "User-Agent": "claude-cli/2.1.158 (external, sdk-cli)",
                    "anthropic-beta": os.environ.get(
                        "AGENT_ROUTER_ANTHROPIC_BETA",
                        "claude-code-20250219,interleaved-thinking-2025-05-14,effort-2025-11-24,redact-thinking-2026-02-12",
                    ),
                    "anthropic-dangerous-direct-browser-access": "true",
                    "x-app": "cli",
                    "X-Stainless-Lang": "python",
                    "X-Stainless-Package-Version": "0.32.1",
                    "X-Stainless-OS": "linux",
                    "X-Stainless-Arch": "amd64",
                    "X-Stainless-Runtime": "Python",
                    "X-Stainless-Runtime-Version": "3.11",
                    "Content-Type": "application/json",
                }
                response = await asyncio.to_thread(
                    requests.post,
                    agent_url,
                    json=payload,
                    headers=headers,
                    timeout=float(os.environ.get("AGENT_ROUTER_TIMEOUT_SECONDS", "300")),
                )
                response_data = _uai_response_data(response)
                if response.status_code < 200 or response.status_code >= 300:
                    logger.warning("Agent router returned HTTP %s: %s", response.status_code, _uai_error_text(response_data)[:500])
                    raise RuntimeError("Agent router request failed")
                reply = _uai_clean_text(_uai_extract_reply(response_data), UAI_MAX_TEXT_CHARS)
                if not reply:
                    logger.warning(
                        "Agent router returned HTTP %s without text; response shape: %s",
                        response.status_code,
                        _uai_shape(response_data),
                    )
                    raise RuntimeError("Agent router returned no text")

                UAI_HISTORIES[chat_id] = (history + [
                    {"role": "user", "text": _uai_clean_text(history_text, UAI_HISTORY_FILE_CHARS)},
                    {"role": "assistant", "text": reply},
                ])[-UAI_HISTORY_MAX_MESSAGES:]
                self.set_status(200)
                self.set_header("Content-Type", "application/json")
                self.write({"success": True, "text": reply})
            except tornado.web.HTTPError:
                raise
            except Exception:
                logger.exception("Direct /api/uai agent-router request failed")
                self.set_status(502)
                self.set_header("Content-Type", "application/json")
                self.write({"success": False, "error": "AI agent request failed. Please try again."})


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
            (r"/api/uai/?", UAIHandler),
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
