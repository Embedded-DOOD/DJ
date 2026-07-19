#!/usr/bin/env python3
"""Metadata embedding using mutagen for MP3 (fast, no subprocess).

ffmpeg is used as a fallback for non-MP3 containers.
Both text tags and cover art are written in a single mutagen operation for MP3,
preserving any previously embedded artwork unless new art is provided.
"""

from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from .utils import clean_meta_value, extract_spotify_track_id, first_artist

try:
    from mutagen.id3 import (
        ID3,
        ID3NoHeaderError,
        APIC,
        TIT2, TPE1, TALB, TPE2, TDRC, TRCK, TPOS, TSRC, COMM, TXXX, WOAS,
    )
    from mutagen.mp3 import MP3
    _MUTAGEN_AVAILABLE = True
except ImportError:
    _MUTAGEN_AVAILABLE = False

ROW_KEY_COLUMN = "row_key"


def build_audio_metadata(
    row: Dict[str, str],
    row_id: Optional[int] = None,
    source_url: Optional[str] = None,
) -> Dict[str, str]:
    metadata: Dict[str, str] = {}

    title = clean_meta_value(row.get("Track Name", ""))
    artist = clean_meta_value(first_artist(row.get("Artist Name(s)", "")))
    album = clean_meta_value(row.get("Album Name", ""))
    album_artist = clean_meta_value(first_artist(row.get("Album Artist Name(s)", "")))
    release_date = clean_meta_value(row.get("Album Release Date", ""))
    track_number = clean_meta_value(row.get("Track Number", ""))
    disc_number = clean_meta_value(row.get("Disc Number", ""))
    isrc = clean_meta_value(row.get("ISRC", ""))
    spotify_track_id = extract_spotify_track_id(row.get("Track URI", ""))
    row_key = clean_meta_value(row.get(ROW_KEY_COLUMN, ""))

    if title:
        metadata["title"] = title
    if artist:
        metadata["artist"] = artist
    if album:
        metadata["album"] = album
    if album_artist:
        metadata["album_artist"] = album_artist
    if release_date:
        metadata["date"] = release_date
    if row_id is not None and row_id > 0:
        metadata["track"] = str(row_id)
    elif track_number and disc_number:
        metadata["track"] = f"{track_number}/{disc_number}"
    elif track_number:
        metadata["track"] = track_number
    if disc_number:
        metadata["disc"] = disc_number
    if isrc:
        metadata["isrc"] = isrc
    if spotify_track_id:
        metadata["spotify_track_id"] = spotify_track_id
    if row_key:
        metadata["row_key"] = row_key
    if row_id is not None and row_id > 0:
        metadata["row_id"] = str(row_id)
    if source_url:
        metadata["source_url"] = source_url.strip()

    parts = []
    if row_id is not None and row_id > 0:
        parts.append(f"row_id={row_id}")
    if row_key:
        parts.append(f"row_key={row_key}")
    if spotify_track_id:
        parts.append(f"spotify_track_id={spotify_track_id}")
    if parts:
        metadata["comment"] = "; ".join(parts)

    return metadata


def _fetch_image_bytes(source: Path | str) -> Optional[bytes]:
    """Load image bytes from a local path or URL."""
    if isinstance(source, Path):
        if source.exists():
            return source.read_bytes()
        return None
    url = str(source).strip()
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read()
    except Exception:
        return None


def embed_audio_metadata(
    file_path: Path,
    metadata: Dict[str, str],
    cover_source: Optional[Path | str] = None,
) -> None:
    """Write tags and optionally cover art in one operation.

    For MP3: uses mutagen (fast, in-place, no subprocess).
    For other formats: falls back to ffmpeg.
    cover_source may be a local Path or a URL string.
    """
    if not metadata and cover_source is None:
        return

    if file_path.suffix.lower() == ".mp3" and _MUTAGEN_AVAILABLE:
        _embed_mp3_mutagen(file_path, metadata, cover_source)
    else:
        _embed_ffmpeg(file_path, metadata, cover_source)


def _embed_mp3_mutagen(
    file_path: Path,
    metadata: Dict[str, str],
    cover_source: Optional[Path | str],
) -> None:
    try:
        tags = ID3(str(file_path))
    except ID3NoHeaderError:
        tags = ID3()

    if metadata.get("title"):
        tags["TIT2"] = TIT2(encoding=3, text=metadata["title"])
    if metadata.get("artist"):
        tags["TPE1"] = TPE1(encoding=3, text=metadata["artist"])
    if metadata.get("album"):
        tags["TALB"] = TALB(encoding=3, text=metadata["album"])
    if metadata.get("album_artist"):
        tags["TPE2"] = TPE2(encoding=3, text=metadata["album_artist"])
    if metadata.get("date"):
        tags["TDRC"] = TDRC(encoding=3, text=metadata["date"])
    if metadata.get("track"):
        tags["TRCK"] = TRCK(encoding=3, text=metadata["track"])
    if metadata.get("disc"):
        tags["TPOS"] = TPOS(encoding=3, text=metadata["disc"])
    if metadata.get("isrc"):
        tags["TSRC"] = TSRC(encoding=3, text=metadata["isrc"])
    if metadata.get("comment"):
        tags["COMM::eng"] = COMM(encoding=3, lang="eng", desc="", text=metadata["comment"])
    for key in ("spotify_track_id", "row_key", "row_id", "source_url"):
        if metadata.get(key):
            tags[f"TXXX:{key}"] = TXXX(encoding=3, desc=key, text=metadata[key])

    # WOAS = Official Audio Source URL — standard ID3 frame, visible in most tag editors.
    if metadata.get("source_url"):
        tags["WOAS"] = WOAS(url=metadata["source_url"])

    # Preserve existing artwork unless new art is supplied.
    existing_apic = tags.get("APIC:") or tags.get("APIC:Cover")
    if cover_source is not None:
        image_bytes = _fetch_image_bytes(cover_source)
        if image_bytes:
            mime = "image/jpeg"
            if isinstance(cover_source, Path) and cover_source.suffix.lower() == ".png":
                mime = "image/png"
            tags.delall("APIC")
            tags["APIC:"] = APIC(
                encoding=3,
                mime=mime,
                type=3,  # Cover (front)
                desc="Cover",
                data=image_bytes,
            )
    elif existing_apic is not None:
        # Re-attach preserved art explicitly so save() keeps it.
        tags["APIC:"] = existing_apic

    tags.save(str(file_path), v2_version=3, v1=2)


def _embed_ffmpeg(
    file_path: Path,
    metadata: Dict[str, str],
    cover_source: Optional[Path | str],
) -> None:
    """Single ffmpeg pass for metadata + optional cover art (non-MP3 fallback)."""
    suffix = file_path.suffix.lower()
    tagged_file = file_path.with_name(f"{file_path.stem}.tagtmp{file_path.suffix}")

    image_bytes: Optional[bytes] = None
    if cover_source is not None:
        image_bytes = _fetch_image_bytes(cover_source)

    cmd = ["ffmpeg", "-y", "-i", str(file_path)]

    if image_bytes:
        # Write image to a temp file so ffmpeg can read it.
        img_tmp = file_path.with_name(f"{file_path.stem}_cover_tmp.jpg")
        img_tmp.write_bytes(image_bytes)
        cmd += ["-i", str(img_tmp)]

    cmd += ["-map_metadata", "0", "-map", "0:a:0"]

    if image_bytes:
        cmd += [
            "-map", "1:v:0",
            "-c:a", "copy",
            "-c:v", "mjpeg",
            "-metadata:s:v:0", "title=Cover",
            "-metadata:s:v:0", "comment=Cover (front)",
            "-disposition:v:0", "attached_pic",
        ]
    else:
        cmd += ["-c", "copy"]

    if suffix == ".mp3":
        cmd += ["-id3v2_version", "3", "-write_id3v1", "1"]
    elif suffix in {".m4a", ".mp4"}:
        cmd += ["-movflags", "+use_metadata_tags"]

    for key, value in metadata.items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    for key in ("title", "artist", "album", "track"):
        if metadata.get(key):
            cmd += ["-metadata:s:a:0", f"{key}={metadata[key]}"]

    cmd.append(str(tagged_file))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "").strip() or "ffmpeg metadata embedding failed")
        tagged_file.replace(file_path)
    finally:
        if image_bytes and 'img_tmp' in dir():
            try:
                img_tmp.unlink(missing_ok=True)
            except Exception:
                pass
        if tagged_file.exists():
            tagged_file.unlink(missing_ok=True)
