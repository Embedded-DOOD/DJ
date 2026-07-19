#!/usr/bin/env python3
"""YouTube fallback search via yt-dlp.

Used when a track cannot be sourced from SoundCloud (DRM, private, or no match).
Search uses the generic ytsearch prefix — works without YouTube Music credentials.
Download reuses sc_interface.download_audio since yt-dlp handles any URL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .sc_interface import build_ydl_options
from .utils import classify_download_error, clean_yt_dlp_error

try:
    from .sc_interface import RateLimitError
except ImportError:
    class RateLimitError(RuntimeError):  # type: ignore
        pass


def search_youtube(
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
    """Search YouTube and return up to `limit` candidate dicts.

    Returns candidates in the same format as search_soundcloud() so the
    existing matcher can score them without modification.
    """
    search_url = f"ytsearch{limit}:{query}"
    options = build_ydl_options(
        cookies_from_browser, cookies_file, sleep_requests,
        limit_rate, throttled_rate, sleep_interval, max_sleep_interval,
    )
    options["playlistend"] = limit

    try:
        with YoutubeDL(options) as ydl:
            payload = ydl.extract_info(search_url, download=False)
    except DownloadError as exc:
        message = clean_yt_dlp_error(str(exc).strip()) or "YouTube search failed"
        if classify_download_error(message) == "rate_limit":
            raise RateLimitError(message) from exc
        raise RuntimeError(message) from exc

    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    out: List[Dict[str, object]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        duration = item.get("duration")
        uploader = str(item.get("uploader") or item.get("channel") or "")
        out.append({
            "duration":    int(duration) if isinstance(duration, (int, float)) else None,
            "title":       str(item.get("title") or ""),
            "webpage_url": str(item.get("webpage_url") or item.get("url") or ""),
            "uploader":    uploader,
        })
    return out
