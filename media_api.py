from __future__ import annotations

import asyncio
import base64
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
import zipfile
from io import BytesIO
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
TG_STICKER_JOBS: dict[str, dict[str, Any]] = {}
TG_STICKER_JOB_TTL_SECONDS = 1800
TG_STICKER_PACK_RE = re.compile(r"^https?://t\.me/addstickers/([A-Za-z0-9_-]+)(?:\?.*)?$", re.IGNORECASE)
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


def _telegram_sticker_pack_name(value: Any) -> str:
    match = TG_STICKER_PACK_RE.match(str(value or "").strip())
    if not match:
        raise MediaAPIError("Use a Telegram sticker-pack URL such as https://t.me/addstickers/PackName.")
    return match.group(1)


def _telegram_api(method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise MediaAPIError("TG_TAG BOT_TOKEN is not configured.")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=dict(payload),
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise MediaAPIError(str(data.get("description") or f"Telegram API {method} failed."))
    return data.get("result") or {}


def _download_telegram_sticker_pack_sync(
    pack_url: str,
    on_sticker: Any = None,
) -> tuple[list[dict[str, Any]], str, str]:
    pack_name = _telegram_sticker_pack_name(pack_url)
    sticker_set = _telegram_api("getStickerSet", {"name": pack_name})
    stickers = sticker_set.get("stickers") or []
    if not stickers:
        raise MediaAPIError("The Telegram sticker pack is empty or unavailable.")
    temp_dir = tempfile.mkdtemp(prefix="tg_tag_stickers_")
    results: list[dict[str, Any]] = []
    try:
        for index, sticker in enumerate(stickers, start=1):
            file_info = _telegram_api("getFile", {"file_id": sticker["file_id"]})
            file_path = str(file_info.get("file_path") or "")
            if not file_path:
                raise MediaAPIError(f"Telegram did not return a file path for sticker {index}.")
            token = os.environ["BOT_TOKEN"].strip()
            response = requests.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                timeout=90,
            )
            response.raise_for_status()
            if len(response.content) > 20 * 1024 * 1024:
                raise MediaAPIError(f"Sticker {index} exceeds the 20 MB limit.")
            suffix = Path(file_path).suffix.lower() or ".bin"
            source_path = os.path.join(temp_dir, f"source-{index}{suffix}")
            output_path = os.path.join(temp_dir, f"sticker-{index}.webp")
            Path(source_path).write_bytes(response.content)
            if suffix == ".webp":
                shutil.copyfile(source_path, output_path)
            elif suffix == ".webm":
                # Heroku's FFmpeg does not provide libwebp_anim. Extract all
                # frames, encode them with cwebp, then mux them with webpmux.
                frames_dir = os.path.join(temp_dir, f"frames-{index}")
                os.makedirs(frames_dir, exist_ok=True)
                frame_conversion = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", source_path,
                        "-vf", "scale=512:512:force_original_aspect_ratio=decrease:flags=lanczos,"
                        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000,format=rgba",
                        "-fps_mode", "passthrough", "-an",
                        os.path.join(frames_dir, "frame-%05d.png"),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=60,
                )
                if frame_conversion.returncode != 0:
                    error_tail = frame_conversion.stderr.decode("utf-8", "replace")[-1200:].strip()
                    raise MediaAPIError(f"WebM frame extraction failed: {error_tail}")
                frame_paths = sorted(Path(frames_dir).glob("frame-*.png"))
                if not frame_paths:
                    raise MediaAPIError(f"WebM sticker {index} produced no frames.")
                webp_frames: list[str] = []
                try:
                    for frame_path in frame_paths:
                        frame_webp = str(frame_path.with_suffix(".webp"))
                        conversion = subprocess.run(
                            ["cwebp", "-quiet", "-q", "75", str(frame_path), "-o", frame_webp],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=30,
                        )
                        if conversion.returncode != 0:
                            error_tail = conversion.stderr.decode("utf-8", "replace")[-1200:].strip()
                            raise MediaAPIError(f"PNG-to-WebP conversion failed: {error_tail}")
                        webp_frames.append(frame_webp)
                    # Keep the source frame rate instead of assuming 30 FPS;
                    # using a fixed duration can make the animation end early
                    # or play at the wrong speed.
                    probe = subprocess.run(
                        [
                            "ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=avg_frame_rate",
                            "-of", "default=nw=1:nk=1", source_path,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=False,
                    )
                    rate = probe.stdout.strip()
                    try:
                        numerator, denominator = (int(value) for value in rate.split("/", 1))
                        frame_duration = max(1, round(1000 * denominator / numerator))
                    except (ValueError, ZeroDivisionError):
                        frame_duration = 33
                    mux_args = ["webpmux"]
                    for frame_webp in webp_frames:
                        # webpmux requires the duration property to use the
                        # explicit '+' prefix, e.g. '+33' milliseconds.
                        mux_args.extend(["-frame", frame_webp, f"+{frame_duration}"])
                    mux_args.extend(["-loop", "0", "-o", output_path])
                    mux = subprocess.run(
                        mux_args,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=60,
                    )
                    if mux.returncode != 0:
                        error_tail = mux.stderr.decode("utf-8", "replace")[-1200:].strip()
                        raise MediaAPIError(f"Animated WebP muxing failed: {error_tail}")
                finally:
                    shutil.rmtree(frames_dir, ignore_errors=True)
            else:
                raise MediaAPIError(
                    f"Sticker {index} is animated (.tgs), which cannot be converted to a WhatsApp sticker by this build."
                )
            item = {
                "index": index,
                "path": output_path,
                "filename": f"sticker-{index}.webp",
                "emoji": sticker.get("emoji") or "",
                "is_animated": bool(sticker.get("is_animated") or sticker.get("is_video")),
            }
            results.append(item)
            if on_sticker is not None:
                on_sticker(item)
        return results, str(sticker_set.get("title") or pack_name), temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


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
    return_info: bool = False,
) -> tuple[str, str] | tuple[str, str, dict[str, Any]]:
    directory = tempfile.mkdtemp(prefix="tg_tag_api_video_")
    try:
        raw_path = os.path.join(directory, "raw.media")
        options = _base_ytdl_options(common_options)
        source_host = urlparse(source_url).netloc.lower().split(":", 1)[0].removeprefix("www.")
        if source_host == "instagram.com" or source_host.endswith(".instagram.com"):
            # Instagram posts commonly expose one MP4 format, not an
            # independent video/audio pair with height metadata.
            format_selector = "best[ext=mp4]/best"
        elif _is_facebook_url(source_url):
            format_selector = f"best[height<={max_height}]/best"
        else:
            format_selector = (
                f"best[ext=mp4][height<={max_height}]/"
                f"bestvideo[ext=mp4][height<={max_height}]+"
                f"bestaudio[ext=m4a]/best[height<={max_height}][ext=mp4]/best"
            )

        options.update(
            {
                "outtmpl": os.path.join(directory, "%(title).120B.%(ext)s"),
                "format": format_selector,
                "merge_output_format": "mp4",
            }
        )
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(source_url, download=True)
        _copy_valid_download(directory, raw_path, require_audio=require_audio)
        if os.path.getsize(raw_path) > MAX_API_FILE_BYTES:
            raise MediaAPIError("The downloaded video is larger than the supported 2 GB limit.")
        if return_info:
            return raw_path, directory, info if isinstance(info, dict) else {}
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


def _cleanup_telegram_sticker_jobs() -> None:
    now = time.time()
    expired = []
    for job_id, job in TG_STICKER_JOBS.items():
        if now - float(job.get("created_at", now)) <= TG_STICKER_JOB_TTL_SECONDS:
            continue
        shutil.rmtree(str(job.get("temp_dir") or ""), ignore_errors=True)
        expired.append(job_id)
    for job_id in expired:
        TG_STICKER_JOBS.pop(job_id, None)


async def _run_telegram_sticker_job(job_id: str, pack_url: Any) -> None:
    job = TG_STICKER_JOBS.get(job_id)
    if not job:
        return
    job["state"] = "processing"
    job["stickers"] = []
    try:
        def publish_sticker(item: dict[str, Any]) -> None:
            job.setdefault("stickers", []).append(item)
            job["updated_at"] = time.time()

        stickers, title, temp_dir = await asyncio.to_thread(
            _download_telegram_sticker_pack_sync,
            pack_url,
            publish_sticker,
        )
        job.update({
            "state": "ready",
            "title": title,
            "temp_dir": temp_dir,
            "stickers": stickers,
        })
    except Exception as exc:
        logger.exception("Telegram sticker job %s failed", job_id)
        job.update({"state": "failed", "error": str(exc)})


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


def _tikwm_image_urls(data: Mapping[str, Any]) -> list[str]:
    """Normalize image fields returned by TikWM API variants."""
    candidates: list[Any] = []
    for field in ("images", "slides", "image", "pics"):
        value = data.get(field)
        if isinstance(value, list):
            candidates.extend(value)
        elif value:
            candidates.append(value)
    image_post_info = data.get("image_post_info")
    if isinstance(image_post_info, Mapping):
        nested_images = image_post_info.get("images") or image_post_info.get("slides") or []
        if isinstance(nested_images, list):
            candidates.extend(nested_images)
    urls: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, str):
            value = item.strip()
            if value.startswith(("http://", "https://")) and value not in urls:
                urls.append(value)
        elif isinstance(item, Mapping):
            for key in ("url", "download_addr", "src", "url_list", "imageURL", "thumbnail"):
                if key in item:
                    collect(item[key])
        elif isinstance(item, list):
            for value in item:
                collect(value)

    for item in candidates:
        collect(item)
    return urls


def _tikwm_video_id(url: str) -> str | None:
    match = re.search(r"/(?:video|photo)/(\d+)", url)
    if match:
        return match.group(1)
    try:
        response = requests.get(url, allow_redirects=True, timeout=10, headers={"User-Agent": MEDIA_USER_AGENT})
        match = re.search(r"/(?:video|photo)/(\d+)", response.url)
        return match.group(1) if match else None
    except requests.RequestException:
        return None


def _tiktok_page_image_urls(url: str) -> list[str]:
    """Extract gallery CDN URLs from TikTok page JSON when TikWM omits them."""
    try:
        response = requests.get(url, headers={"User-Agent": MEDIA_USER_AGENT}, timeout=30)
        response.raise_for_status()
        page = (
            response.text.replace("\\u002F", "/")
            .replace("\\/", "/")
            .replace("&amp;", "&")
        )
        urls: list[str] = []
        for block in re.findall(r"url_list\s*[:=]\s*\[(.*?)\]", page, flags=re.DOTALL):
            for value in re.findall(r"https?://[^\"'\\\s]+", block):
                cleaned = value.rstrip("\\,}")
                if ("tiktok" in cleaned or "ibytedtos" in cleaned) and cleaned not in urls:
                    urls.append(cleaned)
        return urls
    except (requests.RequestException, UnicodeError):
        return []


def _download_tikwm_sync(url: str) -> tuple[str, Any] | None:
    headers = {"User-Agent": MEDIA_USER_AGENT}
    responses = []
    try:
        responses.append(requests.get(
            "https://www.tikwm.com/api/",
            params={"url": url, "hd": "1"},
            headers=headers,
            timeout=30,
        ))
    except requests.RequestException as exc:
        logger.warning("TikWM primary endpoint failed: %s", exc)

    video_id = _tikwm_video_id(url)
    if video_id:
        try:
            responses.append(requests.get(
                "https://www.tikwm.com/api/feed/video",
                params={"video_id": video_id},
                headers=headers,
                timeout=30,
            ))
        except requests.RequestException as exc:
            logger.warning("TikWM feed endpoint failed: %s", exc)

    data: dict[str, Any] = {}
    best_images: list[str] = []
    for response in responses:
        try:
            response.raise_for_status()
            candidate = (response.json() or {}).get("data") or {}
            if isinstance(candidate, dict):
                candidate_images = _tikwm_image_urls(candidate)
                if len(candidate_images) > len(best_images):
                    data = candidate
                    best_images = candidate_images
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("Invalid TikWM response: %s", exc)
    if not data:
        return None
    caption = str(data.get("title") or "")
    images = best_images or _tikwm_image_urls(data)
    if images:
        return "json", {
            "type": "images",
            "count": len(images),
            "urls": images,
            "items": [{"url": image, "caption": caption} for image in images],
            "caption": caption,
        }

    page_images = _tiktok_page_image_urls(url)
    if page_images:
        logger.info("Recovered %s TikTok gallery images from page data", len(page_images))
        return "json", {
            "type": "images",
            "count": len(page_images),
            "urls": page_images,
            "items": [{"url": image, "caption": caption} for image in page_images],
            "caption": caption,
        }

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


def _extract_video_photos_sync(
    source_url: str,
    common_options: Mapping[str, Any],
) -> bytes:
    """Download a video URL and return up to twelve sharp representative JPGs as a ZIP."""
    uploaded_source = os.path.isfile(source_url)
    if uploaded_source:
        video_path = source_url
        temp_dir = tempfile.mkdtemp(prefix="tg_tag_uploaded_frames_")
    else:
        video_path, temp_dir = _download_video_file_sync(
            source_url,
            common_options,
            require_audio=False,
            max_height=2160,
        )
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path,
            ], capture_output=True, text=True, timeout=30, check=False,
        )
        try:
            duration = float(probe.stdout.strip())
        except (ValueError, TypeError):
            duration = 0
        if duration <= 0:
            raise MediaAPIError("Could not read the video duration.")

        # TG_TAG chooses the count from duration: approximately one frame per
        # second, with bounds to avoid flooding chats or exhausting resources.
        frame_count = min(30, max(3, round(duration)))
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            written = 0
            for index in range(frame_count):
                timestamp = duration * (index + 1) / (frame_count + 1)
                frame_path = os.path.join(temp_dir, f"snapshot-{index + 1}.jpg")
                result = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-ss", f"{timestamp:.3f}", "-i", video_path,
                        "-frames:v", "1", "-q:v", "1", frame_path,
                    ], capture_output=True, text=True, timeout=90, check=False,
                )
                if result.returncode == 0 and os.path.isfile(frame_path):
                    output.write(frame_path, arcname=f"snapshot-{written + 1}.jpg")
                    written += 1
            if not written:
                raise MediaAPIError("No snapshots could be extracted from the video.")
        return archive.getvalue()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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


class VideoPhotosHandler(_BaseHandler):
    async def get(self) -> None:
        await self._handle(
            self.get_query_argument("url", default=""),
            self.get_query_argument("format", default="zip"),
        )

    async def post(self) -> None:
        uploaded = self.request.files.get("video", [])
        upload_body = uploaded[0].body if uploaded else None
        content_type = self.request.headers.get("Content-Type", "").lower()
        if upload_body is None and content_type.startswith("video/") and self.request.body:
            upload_body = self.request.body
        if upload_body is not None:
            temp_input = tempfile.NamedTemporaryFile(
                prefix="tg_tag_upload_", suffix=".mp4", delete=False
            )
            temp_input.write(upload_body)
            temp_input.close()
            try:
                archive = await asyncio.to_thread(
                    _extract_video_photos_sync, temp_input.name, self.common_options
                )
                with zipfile.ZipFile(BytesIO(archive)) as source:
                    images = [
                        {
                            "filename": name,
                            "data": base64.b64encode(source.read(name)).decode("ascii"),
                        }
                        for name in source.namelist()
                        if name.lower().endswith(".jpg")
                    ]
                self.set_header("Content-Type", "application/json")
                self.write({"type": "images", "count": len(images), "images": images})
            except Exception:
                logger.exception("/api/video-to-photos upload failed")
                self.set_status(502)
                self.write({"error": "Could not extract photos from the uploaded video."})
            finally:
                try:
                    os.unlink(temp_input.name)
                except FileNotFoundError:
                    pass
            return

        body = self._json_body()
        await self._handle(
            body.get("url") or self.get_body_argument("url", default=""),
            body.get("format") or self.get_body_argument("format", default="zip"),
        )

    async def _handle(self, raw_url: Any, response_format: Any = "zip") -> None:
        try:
            url = _safe_url(raw_url)
            archive = await asyncio.to_thread(
                _extract_video_photos_sync, url, self.common_options
            )
            if str(response_format).lower() == "json":
                with zipfile.ZipFile(BytesIO(archive)) as source:
                    images = [
                        {
                            "filename": name,
                            "data": base64.b64encode(source.read(name)).decode("ascii"),
                        }
                        for name in source.namelist()
                        if name.lower().endswith(".jpg")
                    ]
                self.set_header("Content-Type", "application/json")
                self.write({"type": "images", "count": len(images), "images": images})
                return
            self._write_media(archive, "video-snapshots.zip", "application/zip")
        except tornado.web.HTTPError:
            raise
        except Exception:
            logger.exception("/api/video-to-photos failed")
            self.set_status(502)
            self.write({"error": "Could not extract photos from that video."})


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

                # TikTok video posts: use yt-dlp directly when TikWM does not
                # return a playable file. Gallery posts are returned above as
                # JSON and never reach this branch.
                tiktok_options = dict(self.common_options)
                tiktok_options.update({
                    "http_headers": {
                        "User-Agent": MEDIA_USER_AGENT,
                        "Referer": "https://www.tiktok.com/",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                    "extractor_args": {
                        "tiktok": {"app_name": ["tiktok_web"]},
                    },
                })
                video_path, temp_dir, info = await asyncio.to_thread(
                    _download_video_file_sync,
                    url,
                    tiktok_options,
                    False,
                    _normalize_quality_height(requested_quality),
                    True,
                )
                try:
                    caption = str((info or {}).get("description") or (info or {}).get("title") or "").strip()
                    if caption:
                        self.set_header("X-Media-Caption", quote(caption, safe=""))
                    await self._stream_file(video_path, "tiktok-video.mp4", "video/mp4")
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
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
            if "tiktok.com" in host:
                self.write({
                    "error": (
                        "TikTok could not be downloaded right now. "
                        "The TikTok page or downloader response changed; try the link again later."
                    )
                })
            else:
                self.write({"error": str(exc)})


class TelegramStickerPackHandler(_BaseHandler):
    async def post(self) -> None:
        _cleanup_telegram_sticker_jobs()
        body = self._json_body()
        pack_url = body.get("url") or self.get_body_argument("url", default="")
        job_id = uuid.uuid4().hex
        TG_STICKER_JOBS[job_id] = {
            "created_at": time.time(),
            "state": "queued",
            "pack_url": pack_url,
        }
        asyncio.create_task(_run_telegram_sticker_job(job_id, pack_url))
        self.set_status(202)
        self.set_header("Content-Type", "application/json")
        self.write({
            "job_id": job_id,
            "status_url": f"/api/tg-stickers/{job_id}",
        })

    async def get(self) -> None:
        await self.post()


class TelegramStickerStatusHandler(_BaseHandler):
    async def get(self, job_id: str) -> None:
        _cleanup_telegram_sticker_jobs()
        job = TG_STICKER_JOBS.get(job_id)
        if not job:
            raise tornado.web.HTTPError(404, reason="Sticker job was not found or expired.")
        self.set_header("Content-Type", "application/json")
        self.set_status(200 if job.get("state") in {"ready", "failed"} else 202)
        payload = {
            "job_id": job_id,
            "state": job.get("state"),
            "title": job.get("title", ""),
            "count": len(job.get("stickers", [])),
            "error": job.get("error", ""),
        }
        if job.get("state") in {"processing", "ready"}:
            payload["stickers"] = [
                {
                    "index": item["index"],
                    "emoji": item["emoji"],
                    "is_animated": bool(item.get("is_animated")),
                    "url": f"/api/tg-stickers/{job_id}/{item['index']}",
                }
                for item in job.get("stickers", [])
            ]
        self.write(payload)


class TelegramStickerHandler(_BaseHandler):
    async def get(self, job_id: str, index: str) -> None:
        _cleanup_telegram_sticker_jobs()
        job = TG_STICKER_JOBS.get(job_id)
        if not job:
            raise tornado.web.HTTPError(404, reason="Sticker pack was not found or expired.")
        try:
            item = next(sticker for sticker in job["stickers"] if str(sticker["index"]) == str(index))
        except StopIteration as exc:
            raise tornado.web.HTTPError(404, reason="Sticker was not found.") from exc
        path = str(item["path"])
        if not os.path.isfile(path):
            raise tornado.web.HTTPError(410, reason="Sticker is no longer available.")
        await self._stream_file(path, str(item["filename"]), "image/webp")


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
            (r"/api/video-to-photos/?", VideoPhotosHandler, {"common_options": common_options}),
            (r"/api/tg-stickers/?", TelegramStickerPackHandler, {"common_options": common_options}),
            (r"/api/tg-stickers/([a-f0-9]{32})/?", TelegramStickerStatusHandler, {"common_options": common_options}),
            (r"/api/tg-stickers/([a-f0-9]{32})/([0-9]+)/?", TelegramStickerHandler, {"common_options": common_options}),
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
