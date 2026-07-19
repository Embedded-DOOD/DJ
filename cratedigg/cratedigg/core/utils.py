#!/usr/bin/env python3
"""Utility helpers."""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from yt_dlp.utils import parse_bytes

# ── Output symbols ───────────────────────────────────────────────────────────
SYM_OK    = "✓"
SYM_FAIL  = "✗"
SYM_SKIP  = "~"
SYM_MISS  = "?"
SYM_ARROW = "→"
SYM_WARN  = "!"
DIVIDER   = "─" * 60


def log(message: str) -> None:
    print(message, flush=True)


def log_divider() -> None:
    print(DIVIDER, flush=True)


def shorten_error_message(value: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def first_artist(artists: str) -> str:
    if not artists:
        return ""
    return artists.split(",", 1)[0].strip()


def stable_base_name(row_id: int, artist: str, track: str) -> str:
    """Include row_id to guarantee uniqueness across duplicate titles."""
    merged = f"{row_id:04d} - {artist} - {track}".strip(" -")
    merged = re.sub(r"[\\/:*?\"<>|]", "_", merged)
    merged = re.sub(r"\s+", " ", merged).strip()
    return merged[:160] if merged else f"{row_id:04d}-track"


def parse_rate_limit(value: str, option_name: str) -> Optional[int]:
    if not value:
        return None
    parsed = parse_bytes(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"Invalid {option_name} value: {value}")
    return int(parsed)


# ── Error classification ─────────────────────────────────────────────────────

ERROR_LABELS = {
    "rate_limit":  "rate limited",
    "unavailable": "unavailable",
    "auth":        "login required",
    "network":     "network error",
    "format":      "no audio stream",
    "unknown":     "error",
}

_RATE_LIMIT_PATTERNS = (
    "429", "too many requests", "rate limit", "rate-limited",
    "retry after", "http error 429", "status code 429",
    "the current session has been rate-limited",
)
_UNAVAILABLE_PATTERNS = (
    "this track is not available",
    "track is private",
    "removed by the user",
    "removed by soundcloud",
    "content is not available",
    "no longer available",
    "geo-restricted",
    "not available in your country",
    "403 forbidden",
    "http error 403",
    "404 not found",
    "http error 404",
    "this playlist is private",
    "unable to download webpage",
)
_AUTH_PATTERNS = (
    "login required",
    "premium required",
    "sign in",
    "authentication required",
    "not logged in",
)
_NETWORK_PATTERNS = (
    "connection reset",
    "connection timed out",
    "timed out",
    "name or service not known",
    "network is unreachable",
    "ssl:",
    "certificate verify failed",
    "temporary failure in name resolution",
)
_FORMAT_PATTERNS = (
    "no suitable format",
    "no audio stream",
    "requested format is not available",
    "unable to extract",
    "no media links found",
)


def classify_download_error(message: str) -> str:
    """Return an error category string for a yt-dlp or download error message."""
    lowered = message.lower()
    if any(p in lowered for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if any(p in lowered for p in _UNAVAILABLE_PATTERNS):
        return "unavailable"
    if any(p in lowered for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in lowered for p in _NETWORK_PATTERNS):
        return "network"
    if any(p in lowered for p in _FORMAT_PATTERNS):
        return "format"
    return "unknown"


def clean_yt_dlp_error(message: str) -> str:
    """Strip yt-dlp's noisy prefix (e.g. 'ERROR: [soundcloud] 12345: ') from error messages."""
    cleaned = re.sub(r"^ERROR:\s*", "", message.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^\[[^\]]+\]\s*\S+:\s*", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or message.strip()


def format_error_for_display(raw_message: str) -> str:
    """Return a clean, categorized, human-readable error string."""
    cleaned = clean_yt_dlp_error(raw_message)
    category = classify_download_error(raw_message)
    label = ERROR_LABELS.get(category, "error")
    short = shorten_error_message(cleaned, limit=120)
    return f"[{label}] {short}"


def clean_meta_value(value: str) -> str:
    return value.replace("\x00", "").strip()


def extract_spotify_track_id(track_uri: str) -> str:
    value = clean_meta_value(track_uri)
    if not value:
        return ""
    match = re.search(r"track[:/](?P<id>[A-Za-z0-9]+)", value)
    return match.group("id") if match else ""
