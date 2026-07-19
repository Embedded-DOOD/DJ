#!/usr/bin/env python3
"""Candidate matching and scoring logic.

Source-agnostic: works on any yt-dlp candidate dict with title/duration/uploader.
YouTube-specific bonuses (official audio, topic uploader) have been removed.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .utils import normalize_text

VERSION_KEYWORDS = {
    "remix", "vip", "live", "acoustic", "instrumental",
    "edit", "radio", "extended", "bootleg", "cover",
    "rework", "flip", "mashup",
}

NOISY_KEYWORDS = {
    "nightcore", "sped up", "slowed", "reverb", "bass boosted",
    "8d", "hour", "lyrics", "lyric", "karaoke", "compilation",
}


def has_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and phrase in text


def track_version_keywords(track: str) -> List[str]:
    norm = normalize_text(track)
    return [kw for kw in VERSION_KEYWORDS if has_phrase(norm, kw)]


def count_token_hits(tokens: List[str], *fields: str) -> int:
    if not tokens:
        return 0
    merged = " ".join(fields)
    return sum(1 for t in tokens if t and t in merged)


def token_overlap_ratio(tokens: List[str], *fields: str) -> float:
    if not tokens:
        return 0.0
    return count_token_hits(tokens, *fields) / max(1, len(tokens))


def score_candidate(
    candidate: Dict[str, object],
    expected_duration_s: int,
    artist_tokens: List[str],
    track_tokens: List[str],
    required_versions: List[str],
    tolerance: int,
) -> Optional[Tuple[int, int]]:
    duration = candidate.get("duration")
    if not isinstance(duration, int):
        return None

    delta = abs(duration - expected_duration_s)
    if delta > max(tolerance * 2, 20):
        return None

    title = normalize_text(str(candidate.get("title", "")))
    uploader = normalize_text(str(candidate.get("uploader", "")))

    track_overlap = token_overlap_ratio(track_tokens, title)
    artist_overlap = token_overlap_ratio(artist_tokens, title, uploader)

    if track_overlap < 0.30:
        return None
    if artist_tokens and artist_overlap < 0.20:
        return None

    penalty = 0
    for noisy in NOISY_KEYWORDS:
        if has_phrase(title, noisy):
            penalty += 90

    for version in required_versions:
        if version not in f"{title} {uploader}":
            penalty += 120

    candidate_versions = [v for v in VERSION_KEYWORDS if has_phrase(f"{title} {uploader}", v)]
    for version in candidate_versions:
        if version not in required_versions:
            penalty += 45

    track_hits = count_token_hits(track_tokens, title)
    artist_hits = count_token_hits(artist_tokens, title, uploader)

    score = (
        delta * 35
        - int(track_overlap * 280)
        - int(artist_overlap * 180)
        - (track_hits * 18 + artist_hits * 12)
        + penalty
    )
    return score, delta


def choose_candidate(
    candidates: List[Dict[str, object]],
    expected_duration_s: int,
    artist: str,
    track: str,
    tolerance: int,
) -> Optional[Tuple[Dict[str, object], int]]:
    artist_tokens = [t for t in normalize_text(artist).split() if len(t) >= 3]
    track_tokens = [t for t in normalize_text(track).split() if len(t) >= 3]
    required_versions = track_version_keywords(track)

    best: Optional[Tuple[Dict[str, object], int, int]] = None
    for cand in candidates:
        scored = score_candidate(cand, expected_duration_s, artist_tokens, track_tokens, required_versions, tolerance)
        if scored is None:
            continue
        score, delta = scored
        if best is None or score < best[2]:
            best = (cand, delta, score)

    return (best[0], best[1]) if best is not None else None
