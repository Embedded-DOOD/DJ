#!/usr/bin/env python3
"""CSV source/work file state helpers."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Dict, List, Tuple

from .utils import extract_spotify_track_id, normalize_text

ID_COLUMN = "id"
ROW_KEY_COLUMN = "row_key"

# source_url/matched_title replace youtube_url/selected_title from ExportifyDownloader.
# Old headers are accepted on read for backward compatibility.
TRACKING_COLUMNS = [
    "download_status",
    "artwork_status",
    "source_url",
    "matched_title",
    "selected_duration_s",
    "duration_delta_s",
    "output_file",
    "output_format",
    "attempted_at",
    "error_message",
]

# Accept these legacy column names from ExportifyDownloader work CSVs.
_LEGACY_ALIASES = {
    "youtube_url": "source_url",
    "selected_title": "matched_title",
}


def read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise RuntimeError(f"CSV appears empty or invalid: {path}")
        raw_fieldnames = list(reader.fieldnames)
        raw_rows = [dict(r) for r in reader]

    # Migrate legacy column names in-memory.
    fieldnames = [_LEGACY_ALIASES.get(c, c) for c in raw_fieldnames]
    rows: List[Dict[str, str]] = []
    for raw in raw_rows:
        row = {_LEGACY_ALIASES.get(k, k): v for k, v in raw.items()}
        rows.append(row)

    return fieldnames, rows


def write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)


def row_key_base(row: Dict[str, str]) -> str:
    spotify_id = extract_spotify_track_id(row.get("Track URI", ""))
    if spotify_id:
        return f"sp:{spotify_id}"
    parts = [
        normalize_text(row.get("Track Name", "")),
        normalize_text(row.get("Artist Name(s)", "")),
        normalize_text(row.get("Album Name", "")),
        normalize_text(row.get("Track Duration (ms)", "")),
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"fp:{digest}"


def derive_row_keys(rows: List[Dict[str, str]]) -> None:
    seen: Dict[str, int] = {}
    for row in rows:
        base = row_key_base(row)
        seen[base] = seen.get(base, 0) + 1
        row[ROW_KEY_COLUMN] = base if seen[base] == 1 else f"{base}#{seen[base]}"


def ensure_row_keys(rows: List[Dict[str, str]]) -> None:
    seen: Dict[str, int] = {}
    for row in rows:
        if (row.get(ROW_KEY_COLUMN) or "").strip():
            continue
        base = row_key_base(row)
        seen[base] = seen.get(base, 0) + 1
        row[ROW_KEY_COLUMN] = base if seen[base] == 1 else f"{base}#{seen[base]}"


def ensure_row_ids(rows: List[Dict[str, str]]) -> None:
    next_id = 1
    for row in rows:
        raw = (row.get(ID_COLUMN) or "").strip()
        if raw.isdigit() and int(raw) > 0:
            next_id = max(next_id, int(raw) + 1)
    for row in rows:
        raw = (row.get(ID_COLUMN) or "").strip()
        if raw.isdigit() and int(raw) > 0:
            row[ID_COLUMN] = str(int(raw))
        else:
            row[ID_COLUMN] = str(next_id)
            next_id += 1


def ensure_tracking_columns(fieldnames: List[str]) -> List[str]:
    updated = list(fieldnames)
    for col in [ID_COLUMN, ROW_KEY_COLUMN, *TRACKING_COLUMNS]:
        if col not in updated:
            updated.append(col)
    # id always first
    updated = [c for c in updated if c != ID_COLUMN]
    return [ID_COLUMN, *updated]


def ensure_all_columns(rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    for row in rows:
        for col in fieldnames:
            row.setdefault(col, "")


def work_csv_path_for(source_csv_path: Path) -> Path:
    return source_csv_path.with_name(f"{source_csv_path.stem}_work{source_csv_path.suffix}")


def playlist_stem(csv_path: Path) -> str:
    stem = csv_path.stem
    if stem.lower().endswith("_work"):
        return stem[:-5]
    return stem


def prepare_work_csv(source_csv_path: Path) -> Path:
    work_csv_path = work_csv_path_for(source_csv_path)
    source_fieldnames, source_rows = read_csv(source_csv_path)
    derive_row_keys(source_rows)
    source_keyed: Dict[str, Dict[str, str]] = {r[ROW_KEY_COLUMN]: r for r in source_rows}

    if not work_csv_path.exists():
        fieldnames = ensure_tracking_columns(list(source_fieldnames))
        rows_to_write: List[Dict[str, str]] = []
        for source_row in source_rows:
            new_row = {col: source_row.get(col, "") for col in source_fieldnames}
            new_row[ROW_KEY_COLUMN] = source_row[ROW_KEY_COLUMN]
            for col in TRACKING_COLUMNS:
                new_row[col] = ""
            rows_to_write.append(new_row)
        ensure_row_ids(rows_to_write)
        ensure_all_columns(rows_to_write, fieldnames)
        write_csv(work_csv_path, fieldnames, rows_to_write)
        print(f"Created work CSV: {work_csv_path.name}")
        return work_csv_path

    work_fieldnames, work_rows = read_csv(work_csv_path)
    for col in source_fieldnames:
        if col not in work_fieldnames:
            work_fieldnames.append(col)
    work_fieldnames = ensure_tracking_columns(work_fieldnames)

    for row in work_rows:
        row.setdefault(ID_COLUMN, "")
        row.setdefault(ROW_KEY_COLUMN, "")

    if any(not (row.get(ROW_KEY_COLUMN) or "").strip() for row in work_rows):
        derive_row_keys(work_rows)

    ensure_row_ids(work_rows)
    work_by_key: Dict[str, Dict[str, str]] = {
        (r.get(ROW_KEY_COLUMN) or "").strip(): r
        for r in work_rows
        if (r.get(ROW_KEY_COLUMN) or "").strip()
    }

    added = 0
    for key, source_row in source_keyed.items():
        if key in work_by_key:
            for col in source_fieldnames:
                work_by_key[key][col] = source_row.get(col, "")
        else:
            new_row = {col: "" for col in work_fieldnames}
            for col in source_fieldnames:
                new_row[col] = source_row.get(col, "")
            new_row[ROW_KEY_COLUMN] = key
            work_rows.append(new_row)
            added += 1

    ensure_row_ids(work_rows)
    ensure_all_columns(work_rows, work_fieldnames)
    write_csv(work_csv_path, work_fieldnames, work_rows)

    if added:
        print(f"Synced work CSV: {work_csv_path.name} (+{added} new rows)")
    else:
        print(f"Synced work CSV: {work_csv_path.name} (no new rows)")

    return work_csv_path


# ── SC-playlist-native mode ───────────────────────────────────────────────────

# Columns used when input is a SoundCloud URL (no Spotify metadata).
SC_PLAYLIST_SOURCE_COLUMNS = [
    "Track Name",
    "Artist Name(s)",
    "Track Duration (ms)",
    "sc_track_id",
    "sc_thumbnail_url",
]


def prepare_sc_playlist_csv(
    playlist_name: str,
    output_dir: Path,
    tracks: List[Dict[str, object]],
) -> Path:
    """Create or update a work CSV from a SoundCloud playlist extraction.

    Pre-populates `source_url` so the downloader skips the search step
    and downloads each track directly.
    """
    work_csv_path = output_dir / f"{playlist_name}_work.csv"
    fieldnames = ensure_tracking_columns(list(SC_PLAYLIST_SOURCE_COLUMNS))

    def _make_row(idx: int, track: Dict[str, object]) -> Dict[str, str]:
        duration_ms = track.get("duration_ms")
        return {
            ID_COLUMN:             str(idx),
            ROW_KEY_COLUMN:        f"sc:{track['sc_track_id']}" if track.get("sc_track_id") else f"fp:{idx}",
            "Track Name":          str(track.get("title") or "").strip(),
            "Artist Name(s)":      str(track.get("uploader") or "").strip(),
            "Track Duration (ms)": str(int(duration_ms)) if isinstance(duration_ms, int) else "",
            "sc_track_id":         str(track.get("sc_track_id") or "").strip(),
            "sc_thumbnail_url":    str(track.get("thumbnail_url") or "").strip(),
            # Pre-populate so the downloader skips the search step entirely.
            "source_url":          str(track.get("url") or "").strip(),
            "matched_title":       str(track.get("title") or "").strip(),
            "selected_duration_s": str(int(duration_ms) // 1000) if isinstance(duration_ms, int) else "",
            "duration_delta_s":    "0",
            "download_status":     "",
            "artwork_status":      "",
            "output_file":         "",
            "output_format":       "",
            "attempted_at":        "",
            "error_message":       "",
        }

    if not work_csv_path.exists():
        rows = [_make_row(i + 1, t) for i, t in enumerate(tracks)]
        ensure_all_columns(rows, fieldnames)
        write_csv(work_csv_path, fieldnames, rows)
        print(f"Created SC playlist CSV: {work_csv_path.name} ({len(rows)} tracks)")
        return work_csv_path

    # Resume: append tracks not already present (matched by sc_track_id).
    existing_fieldnames, existing_rows = read_csv(work_csv_path)
    for col in fieldnames:
        if col not in existing_fieldnames:
            existing_fieldnames.append(col)

    existing_keys = {
        (r.get(ROW_KEY_COLUMN) or "").strip()
        for r in existing_rows
        if (r.get(ROW_KEY_COLUMN) or "").strip()
    }
    next_id = max(
        (int(r[ID_COLUMN]) for r in existing_rows if (r.get(ID_COLUMN) or "").strip().isdigit()),
        default=0,
    ) + 1

    added = 0
    for track in tracks:
        key = f"sc:{track['sc_track_id']}" if track.get("sc_track_id") else ""
        if key and key in existing_keys:
            continue
        new_row = _make_row(next_id, track)
        existing_rows.append(new_row)
        next_id += 1
        added += 1

    ensure_all_columns(existing_rows, existing_fieldnames)
    write_csv(work_csv_path, existing_fieldnames, existing_rows)

    msg = f"+{added} new tracks" if added else "no new tracks"
    print(f"Synced SC playlist CSV: {work_csv_path.name} ({msg})")
    return work_csv_path
