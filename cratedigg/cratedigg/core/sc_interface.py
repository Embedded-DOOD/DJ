#!/usr/bin/env python3
"""SoundCloud interface via yt-dlp.

Key differences from ExportifyDownloader's yt_dlp_interface:
- Uses scsearch{N}: prefix instead of YouTube Music search URL.
- download_audio returns (Path, thumbnail_url) tuple — no second round-trip.
- Does NOT force MP3 re-encoding by default. Preserves the native SoundCloud
  stream (typically 256kbps AAC with Go+). Pass prefer_mp3=True to transcode.
- No YouTube-specific extractor_args.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .utils import classify_download_error, clean_yt_dlp_error, parse_rate_limit

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".mp4", ".aac", ".flac", ".wav", ".ogg", ".opus"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class RateLimitError(RuntimeError):
    pass


def build_ydl_options(
    cookies_from_browser: str = "",
    cookies_file: Optional[Path] = None,
    sleep_requests: float = 0.0,
    limit_rate: str = "",
    throttled_rate: str = "",
    sleep_interval: float = 0.0,
    max_sleep_interval: float = 0.0,
) -> Dict[str, object]:
    options: Dict[str, object] = {"quiet": True, "no_warnings": True}

    if cookies_from_browser:
        options["cookiesfrombrowser"] = (cookies_from_browser, None, None, None)
    if cookies_file is not None:
        options["cookiefile"] = str(cookies_file)

    if sleep_requests > 0:
        options["sleep_requests"] = sleep_requests
    if sleep_interval > 0:
        options["sleep_interval"] = sleep_interval
    if max_sleep_interval > 0:
        options["max_sleep_interval"] = max_sleep_interval

    limit_rate_bps = parse_rate_limit(limit_rate, "--limit-rate")
    if limit_rate_bps is not None:
        options["ratelimit"] = limit_rate_bps

    throttled_rate_bps = parse_rate_limit(throttled_rate, "--throttled-rate")
    if throttled_rate_bps is not None:
        options["throttledratelimit"] = throttled_rate_bps

    return options


def _extract_thumbnail_url(info: Dict) -> Optional[str]:
    """Pull the best thumbnail URL from a yt-dlp info dict."""
    thumb = info.get("thumbnail")
    if isinstance(thumb, str) and thumb.strip():
        return thumb.strip()

    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        best_url, best_score = "", -1
        for item in thumbnails:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            score = (item.get("width") or 0) + (item.get("height") or 0)
            if score >= best_score:
                best_score = score
                best_url = url.strip()
        if best_url:
            return best_url

    return None


_RATE_LIMIT_BACKOFF = (30, 60, 120)  # seconds to wait on successive 429s


def search_soundcloud(
    query: str,
    limit: int,
    cookies_from_browser: str = "",
    cookies_file: Optional[Path] = None,
    sleep_requests: float = 0.0,
    limit_rate: str = "",
    throttled_rate: str = "",
    sleep_interval: float = 0.0,
    max_sleep_interval: float = 0.0,
) -> List[Dict[str, object]]:
    """Search SoundCloud and return up to `limit` candidate dicts.

    Retries up to 3 times with increasing backoff on 429 rate limits
    before raising RateLimitError to the caller.
    """
    search_url = f"scsearch{limit}:{query}"
    options = build_ydl_options(
        cookies_from_browser, cookies_file, sleep_requests,
        limit_rate, throttled_rate, sleep_interval, max_sleep_interval,
    )
    options["playlistend"] = limit

    last_exc: Exception = RuntimeError("SoundCloud search failed")
    for attempt, backoff in enumerate((*_RATE_LIMIT_BACKOFF, None), start=1):
        try:
            with YoutubeDL(options) as ydl:
                payload = ydl.extract_info(search_url, download=False)
            break  # success
        except DownloadError as exc:
            message = clean_yt_dlp_error(str(exc).strip()) or "SoundCloud search failed"
            if classify_download_error(message) == "rate_limit":
                last_exc = RateLimitError(message)
                if backoff is not None:
                    from .utils import log, SYM_WARN
                    log(f"  {SYM_WARN} Rate limited — waiting {backoff}s before retry ({attempt}/{len(_RATE_LIMIT_BACKOFF)})...")
                    time.sleep(backoff)
                    continue
                raise RateLimitError(message) from exc
            raise RuntimeError(message) from exc
    else:
        raise last_exc

    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    out: List[Dict[str, object]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        duration = item.get("duration")
        out.append({
            "duration": int(duration) if isinstance(duration, (int, float)) else None,
            "title": str(item.get("title") or ""),
            "webpage_url": str(item.get("webpage_url") or item.get("url") or ""),
            "uploader": str(item.get("uploader") or ""),
        })
    return out


def download_audio(
    url: str,
    output_template: str,
    prefer_mp3: bool = False,
    cookies_from_browser: str = "",
    cookies_file: Optional[Path] = None,
    sleep_requests: float = 0.0,
    limit_rate: str = "",
    throttled_rate: str = "",
    sleep_interval: float = 0.0,
    max_sleep_interval: float = 0.0,
) -> Tuple[Optional[Path], Optional[str]]:
    """Download audio from a SoundCloud URL.

    Returns (output_path, thumbnail_url). Either may be None on failure.
    Preserves the native stream format unless prefer_mp3=True.
    """
    output_path = Path(output_template)
    options = build_ydl_options(
        cookies_from_browser, cookies_file, sleep_requests,
        limit_rate, throttled_rate, sleep_interval, max_sleep_interval,
    )
    options.update({
        "format": "bestaudio/best",
        "noplaylist": True,
        "paths": {"home": str(output_path.parent)},
        "outtmpl": {"default": output_path.name},
    })

    if prefer_mp3:
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }]

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            if not isinstance(info, dict):
                return None, None
            thumbnail_url = _extract_thumbnail_url(info)
            final_path = Path(ydl.prepare_filename(info))
    except DownloadError as exc:
        message = clean_yt_dlp_error(str(exc).strip()) or "SoundCloud download failed"
        if classify_download_error(message) == "rate_limit":
            raise RateLimitError(message) from exc
        raise RuntimeError(message) from exc

    # Resolve actual file (extension may differ from template after postprocessing).
    if prefer_mp3:
        mp3_path = final_path.with_suffix(".mp3")
        if mp3_path.exists():
            return mp3_path, thumbnail_url

    if final_path.exists():
        return final_path, thumbnail_url

    # Fallback: scan output dir for any audio file matching the stem.
    resolved = resolve_downloaded_file(output_path.parent, output_path.stem)
    return resolved, thumbnail_url


def resolve_downloaded_file(output_dir: Path, base_name: str) -> Optional[Path]:
    files = sorted(
        [p for p in output_dir.glob(f"{base_name}.*") if p.suffix.lower() in AUDIO_EXTENSIONS],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def resolve_thumbnail_file(output_dir: Path, base_name: str) -> Optional[Path]:
    files = sorted(
        [p for p in output_dir.glob(f"{base_name}.*") if p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def extract_sc_playlist(
    playlist_url: str,
    cookies_from_browser: str = "",
    cookies_file: Optional[Path] = None,
    sleep_requests: float = 0.0,
) -> List[Dict[str, object]]:
    """Extract all track metadata from a SoundCloud playlist or user URL.

    Returns a list of track dicts with:
      url, title, uploader, duration_ms, thumbnail_url, sc_track_id
    Does not download audio — metadata extraction only.
    """
    options = build_ydl_options(cookies_from_browser, cookies_file, sleep_requests)
    options.update({
        "extract_flat": False,   # We want full info per track, not just URLs.
        "playlistend": 0,        # 0 = no limit.
        "ignoreerrors": True,    # Skip unavailable tracks rather than aborting.
    })

    try:
        with YoutubeDL(options) as ydl:
            payload = ydl.extract_info(playlist_url, download=False)
    except DownloadError as exc:
        message = clean_yt_dlp_error(str(exc).strip()) or "Failed to extract SoundCloud playlist"
        if classify_download_error(message) == "rate_limit":
            raise RateLimitError(message) from exc
        raise RuntimeError(message) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected response from SoundCloud — is the URL valid?")

    entries = payload.get("entries")
    if not isinstance(entries, list):
        # Single track URL — wrap it.
        entries = [payload]

    tracks: List[Dict[str, object]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        url = str(item.get("webpage_url") or item.get("url") or "").strip()
        if not url:
            continue
        duration_s = item.get("duration")
        duration_ms = int(duration_s * 1000) if isinstance(duration_s, (int, float)) else None
        tracks.append({
            "url":          url,
            "title":        str(item.get("title") or "").strip(),
            "uploader":     str(item.get("uploader") or item.get("channel") or "").strip(),
            "duration_ms":  duration_ms,
            "thumbnail_url": _extract_thumbnail_url(item) or "",
            "sc_track_id":  str(item.get("id") or "").strip(),
        })

    return tracks
