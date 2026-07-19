#!/usr/bin/env python3
"""Main download loop for cratedigg.

Design principles vs ExportifyDownloader:
- Single-writer CSV: worker threads return result dicts; only the main thread
  mutates `rows` and flushes to disk.
- Batch CSV writes: flushed every FLUSH_INTERVAL completions and on clean exit.
- Parallel downloads via ThreadPoolExecutor (configurable --workers).
- download_audio returns (Path, thumbnail_url) — no second yt-dlp round-trip.
- Native format preserved by default; MP3 transcoding is opt-in (--mp3).
- Filename includes row_id to prevent collisions on duplicate titles.
- None file after download is an error, not a silent success.
- Rate limit stops submission immediately; in-flight futures are collected.
- End-of-run summary lists every failed/unresolved track by name.
"""

from __future__ import annotations

import os
import signal
import sys
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .csv_work_state import (
    ID_COLUMN,
    ensure_all_columns,
    ensure_row_ids,
    ensure_row_keys,
    ensure_tracking_columns,
    playlist_stem,
    write_csv,
)
from .matcher import choose_candidate
from .metadata import build_audio_metadata, embed_audio_metadata
from .sc_interface import (
    RateLimitError,
    download_audio,
    search_soundcloud,
)
from .yt_interface import search_youtube
from .utils import (
    DIVIDER,
    SYM_ARROW,
    SYM_FAIL,
    SYM_MISS,
    SYM_OK,
    SYM_SKIP,
    SYM_WARN,
    classify_download_error,
    first_artist,
    format_error_for_display,
    log,
    log_divider,
    shorten_error_message,
    stable_base_name,
    utc_now,
)

FLUSH_INTERVAL = 5

STATUS_RESOLVED   = "resolved"
STATUS_DOWNLOADED = "downloaded"
STATUS_UNRESOLVED = "unresolved"
STATUS_ERROR      = "error"
STATUS_RETRY      = "retry"

REQUIRED_COLUMNS = ["Track Name", "Artist Name(s)", "Track Duration (ms)"]


@dataclass
class RowResult:
    """Immutable result returned by a worker thread."""
    row_index: int
    status: str
    source_url: str = ""
    matched_title: str = ""
    selected_duration_s: str = ""
    duration_delta_s: str = ""
    output_file: str = ""
    output_format: str = ""
    artwork_status: str = ""
    attempted_at: str = ""
    error_message: str = ""
    thumbnail_url: str = ""


@dataclass
class FailedRow:
    """Collected at end of run for the failure summary."""
    row_id: int
    artist: str
    track: str
    status: str          # unresolved | error | retry
    reason: str          # display-ready message


@dataclass
class RunSettings:
    duration_tolerance: int = 10
    search_results: int = 6
    download_enabled: bool = True
    force_redownload: bool = False
    limit: int = 0
    workers: int = 1
    prefer_mp3: bool = True
    sleep_requests: float = 2.0
    limit_rate: str = ""
    throttled_rate: str = ""
    sleep_interval: float = 0.0
    max_sleep_interval: float = 0.0
    id_order: str = "priority"
    cookies_from_browser: str = ""
    cookies_file: Optional[Path] = None
    yt_fallback: bool = True


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ydl_kwargs(s: RunSettings) -> Dict:
    return dict(
        cookies_from_browser=s.cookies_from_browser,
        cookies_file=s.cookies_file,
        sleep_requests=s.sleep_requests,
        limit_rate=s.limit_rate,
        throttled_rate=s.throttled_rate,
        sleep_interval=s.sleep_interval,
        max_sleep_interval=s.max_sleep_interval,
    )


def _row_id_value(row: Dict[str, str]) -> Optional[int]:
    v = (row.get(ID_COLUMN) or "").strip()
    return int(v) if v.isdigit() else None


def _priority_bucket(row: Dict[str, str]) -> int:
    status = (row.get("download_status") or "").strip().lower()
    if not status:
        return 0
    if status == STATUS_RETRY:
        return 2
    return 1


def _processing_order(rows: List[Dict[str, str]], id_order: str) -> List[int]:
    order = list(range(len(rows)))
    if id_order == "default":
        return order
    if id_order == "priority":
        return sorted(order, key=lambda i: (
            _priority_bucket(rows[i]),
            _row_id_value(rows[i]) is None,
            _row_id_value(rows[i]) or 0,
            i,
        ))
    if id_order == "ascending":
        return sorted(order, key=lambda i: (
            _row_id_value(rows[i]) is None,
            _row_id_value(rows[i]) or 0,
            i,
        ))
    return sorted(order, key=lambda i: (
        _row_id_value(rows[i]) is None,
        -(_row_id_value(rows[i]) or 0),
        i,
    ))


def _should_skip(row: Dict[str, str], force: bool) -> bool:
    if force:
        return False
    status = (row.get("download_status") or "").strip().lower()
    output_file = (row.get("output_file") or "").strip()
    return status == STATUS_DOWNLOADED and bool(output_file) and Path(output_file).exists()


def _has_saved_resolution(row: Dict[str, str]) -> bool:
    return bool((row.get("source_url") or "").strip())


def _apply_result(row: Dict[str, str], result: RowResult) -> None:
    """Apply a RowResult onto the shared row dict (main thread only)."""
    row["download_status"]    = result.status
    row["source_url"]         = result.source_url
    row["matched_title"]      = result.matched_title
    row["selected_duration_s"] = result.selected_duration_s
    row["duration_delta_s"]   = result.duration_delta_s
    row["output_file"]        = result.output_file
    row["output_format"]      = result.output_format
    row["artwork_status"]     = result.artwork_status
    row["attempted_at"]       = result.attempted_at
    row["error_message"]      = result.error_message


def _tag(n: int, total: int) -> str:
    """Return a zero-padded progress tag like [03/47]."""
    w = len(str(total))
    return f"[{n:0{w}d}/{total}]"


# ── YouTube fallback (runs in worker thread) ─────────────────────────────────

def _yt_fallback(
    row_index: int,
    row_id: int,
    row: Dict[str, str],
    output_dir: Path,
    settings: RunSettings,
    tag: str,
    label: str,
    expected_s: int,
    artist: str,
    track: str,
) -> RowResult:
    """Attempt to find and download a track from YouTube after SoundCloud fails."""
    kwargs = _ydl_kwargs(settings)

    log(f"{tag}   search   {label}  [yt fallback]")
    try:
        candidates = search_youtube(
            f"{artist} {track}", settings.search_results, **kwargs
        )
    except RateLimitError:
        raise
    except Exception as exc:
        err = format_error_for_display(str(exc))
        log(f"{tag} {SYM_FAIL} yt search {label}\n         {err}")
        return RowResult(
            row_index=row_index, status=STATUS_UNRESOLVED,
            attempted_at=utc_now(),
            error_message=f"[yt] {str(exc).strip()[:400]}",
        )

    picked = choose_candidate(
        candidates, expected_s, artist, track, settings.duration_tolerance
    )
    if picked is None:
        log(f"{tag} {SYM_MISS} no match {label}  [yt]")
        return RowResult(
            row_index=row_index, status=STATUS_UNRESOLVED,
            attempted_at=utc_now(),
            error_message=f"No YouTube match within {settings.duration_tolerance}s",
        )

    candidate, delta = picked
    url           = str(candidate.get("webpage_url") or "").strip()
    matched_title = str(candidate.get("title") or "").strip()
    cand_dur      = candidate.get("duration")
    sel_dur       = str(cand_dur) if isinstance(cand_dur, int) else ""
    delta_s       = str(delta)

    if not url:
        return RowResult(
            row_index=row_index, status=STATUS_UNRESOLVED,
            attempted_at=utc_now(), error_message="[yt] Matched candidate missing URL",
        )

    log(f"{tag} {SYM_ARROW} matched  {label}  →  {shorten_error_message(matched_title, 60)}  (Δ{delta}s) [yt]")

    if not settings.download_enabled:
        return RowResult(
            row_index=row_index, status=STATUS_RESOLVED,
            source_url=url, matched_title=matched_title,
            selected_duration_s=sel_dur, duration_delta_s=delta_s,
            attempted_at=utc_now(),
        )

    base_name = stable_base_name(row_id, artist, track)
    output_template = str(output_dir / f"{base_name}.%(ext)s")
    log(f"{tag}   fetch    {label}  [yt]")

    try:
        saved_file, thumbnail_url = download_audio(
            url, output_template, prefer_mp3=settings.prefer_mp3, **kwargs
        )
    except RateLimitError:
        raise
    except Exception as exc:
        err = format_error_for_display(str(exc))
        log(f"{tag} {SYM_FAIL} yt fetch {label}\n         {err}")
        return RowResult(
            row_index=row_index, status=STATUS_ERROR,
            source_url=url, matched_title=matched_title,
            selected_duration_s=sel_dur, duration_delta_s=delta_s,
            attempted_at=utc_now(), error_message=f"[yt] {str(exc).strip()[:400]}",
        )

    if saved_file is None:
        msg = "[yt] Download completed but output file not found on disk"
        log(f"{tag} {SYM_FAIL} yt fetch {label}\n         [error] {msg}")
        return RowResult(
            row_index=row_index, status=STATUS_ERROR,
            source_url=url, matched_title=matched_title,
            selected_duration_s=sel_dur, duration_delta_s=delta_s,
            attempted_at=utc_now(), error_message=msg,
        )

    artwork_status = ""
    cover: Optional[str] = thumbnail_url if thumbnail_url else None
    try:
        embed_audio_metadata(
            saved_file, build_audio_metadata(row, row_id, source_url=url),
            cover_source=cover,
        )
        if cover:
            artwork_status = "embedded"
    except Exception as exc:
        log(f"{tag} {SYM_WARN} metadata {label}\n         {shorten_error_message(str(exc), 100)}")

    fmt = saved_file.suffix.lstrip(".")
    log(f"{tag} {SYM_OK} done     {label}  [yt {fmt}]")

    return RowResult(
        row_index=row_index, status=STATUS_DOWNLOADED,
        source_url=url, matched_title=matched_title,
        selected_duration_s=sel_dur, duration_delta_s=delta_s,
        output_file=str(saved_file.resolve()),
        output_format=fmt,
        artwork_status=artwork_status,
        thumbnail_url=thumbnail_url or "",
        attempted_at=utc_now(),
    )


# ── Worker (runs in thread) ──────────────────────────────────────────────────

def _worker(
    row_index: int,
    row: Dict[str, str],
    output_dir: Path,
    settings: RunSettings,
    tag: str,
) -> RowResult:
    """Execute one row. Returns RowResult; never mutates shared state."""
    track   = (row.get("Track Name") or "").strip()
    artists = (row.get("Artist Name(s)") or "").strip()
    artist  = first_artist(artists)
    dur_raw = (row.get("Track Duration (ms)") or "").strip()
    row_id  = _row_id_value(row) or (row_index + 1)

    if not track or not artist or not dur_raw.isdigit():
        return RowResult(
            row_index=row_index, status=STATUS_ERROR,
            attempted_at=utc_now(),
            error_message="[bad data] missing track name, artist, or duration in CSV",
        )

    expected_s = round(int(dur_raw) / 1000)
    label = f"{artist} - {track}"
    kwargs = _ydl_kwargs(settings)

    # ── Resolve SoundCloud URL ───────────────────────────────────────────────
    if _has_saved_resolution(row) and not settings.force_redownload:
        url           = (row.get("source_url") or "").strip()
        matched_title = (row.get("matched_title") or "").strip()
        sel_dur       = (row.get("selected_duration_s") or "").strip()
        delta_s       = (row.get("duration_delta_s") or "").strip()
        log(f"{tag} {SYM_ARROW} resume   {label}")
    else:
        log(f"{tag}   search   {label}")
        try:
            candidates = search_soundcloud(
                f"{artist} {track}", settings.search_results, **kwargs
            )
        except RateLimitError:
            raise
        except Exception as exc:
            err_str = str(exc).strip()
            err_cat = classify_download_error(err_str)
            if err_cat == "rate_limit":
                raise RateLimitError(err_str)
            if err_cat == "unavailable" and settings.yt_fallback:
                log(f"{tag} {SYM_WARN} SC unavailable ({shorten_error_message(err_str, 50)}) — trying YouTube")
                return _yt_fallback(
                    row_index, row_id, row, output_dir, settings,
                    tag, label, expected_s, artist, track,
                )
            err = format_error_for_display(err_str)
            log(f"{tag} {SYM_FAIL} search   {label}\n         {err}")
            return RowResult(
                row_index=row_index, status=STATUS_ERROR,
                attempted_at=utc_now(), error_message=err_str[:400],
            )

        picked = choose_candidate(
            candidates, expected_s, artist, track, settings.duration_tolerance
        )
        if picked is None:
            if settings.yt_fallback:
                return _yt_fallback(
                    row_index, row_id, row, output_dir, settings,
                    tag, label, expected_s, artist, track,
                )
            log(f"{tag} {SYM_MISS} no match {label}\n"
                f"         no SoundCloud result within {settings.duration_tolerance}s")
            return RowResult(
                row_index=row_index, status=STATUS_UNRESOLVED,
                attempted_at=utc_now(),
                error_message=f"No SoundCloud match within {settings.duration_tolerance}s",
            )

        candidate, delta = picked
        url           = str(candidate.get("webpage_url") or "").strip()
        matched_title = str(candidate.get("title") or "").strip()
        cand_dur      = candidate.get("duration")
        sel_dur       = str(cand_dur) if isinstance(cand_dur, int) else ""
        delta_s       = str(delta)

        if not url:
            log(f"{tag} {SYM_FAIL} resolve  {label}\n         matched candidate has no URL")
            return RowResult(
                row_index=row_index, status=STATUS_ERROR,
                attempted_at=utc_now(), error_message="Matched candidate missing URL",
            )

        sc_label = shorten_error_message(matched_title, limit=60)
        log(f"{tag} {SYM_ARROW} matched  {label}  →  {sc_label}  (Δ{delta}s)")

        if not settings.download_enabled:
            return RowResult(
                row_index=row_index, status=STATUS_RESOLVED,
                source_url=url, matched_title=matched_title,
                selected_duration_s=sel_dur, duration_delta_s=delta_s,
                attempted_at=utc_now(),
            )

    # ── Download ─────────────────────────────────────────────────────────────
    base_name = stable_base_name(row_id, artist, track)
    output_template = str(output_dir / f"{base_name}.%(ext)s")
    log(f"{tag}   fetch    {label}")

    try:
        saved_file, thumbnail_url = download_audio(
            url, output_template, prefer_mp3=settings.prefer_mp3, **kwargs
        )
    except RateLimitError:
        raise
    except Exception as exc:
        err_category = classify_download_error(str(exc))
        if err_category == "unavailable" and settings.yt_fallback:
            log(f"{tag} {SYM_WARN} SC unavailable ({shorten_error_message(str(exc), 60)}) — trying YouTube")
            return _yt_fallback(
                row_index, row_id, row, output_dir, settings,
                tag, label, expected_s, artist, track,
            )
        err = format_error_for_display(str(exc))
        log(f"{tag} {SYM_FAIL} download {label}\n         {err}")
        return RowResult(
            row_index=row_index, status=STATUS_ERROR,
            source_url=url, matched_title=matched_title,
            selected_duration_s=sel_dur, duration_delta_s=delta_s,
            attempted_at=utc_now(), error_message=str(exc).strip()[:400],
        )

    if saved_file is None:
        msg = "Download completed but output file not found on disk"
        log(f"{tag} {SYM_FAIL} download {label}\n         [error] {msg}")
        return RowResult(
            row_index=row_index, status=STATUS_ERROR,
            source_url=url, matched_title=matched_title,
            selected_duration_s=sel_dur, duration_delta_s=delta_s,
            attempted_at=utc_now(), error_message=msg,
        )

    # ── Metadata + cover art (single operation) ───────────────────────────────
    artwork_status = ""
    cover: Optional[str] = thumbnail_url if thumbnail_url else None
    try:
        embed_audio_metadata(saved_file, build_audio_metadata(row, row_id, source_url=url), cover_source=cover)
        if cover:
            artwork_status = "embedded"
    except Exception as exc:
        log(f"{tag} {SYM_WARN} metadata {label}\n         {shorten_error_message(str(exc), 100)}")

    fmt = saved_file.suffix.lstrip(".")
    log(f"{tag} {SYM_OK} done     {label}  [{fmt}]")

    return RowResult(
        row_index=row_index, status=STATUS_DOWNLOADED,
        source_url=url, matched_title=matched_title,
        selected_duration_s=sel_dur, duration_delta_s=delta_s,
        output_file=str(saved_file.resolve()),
        output_format=fmt,
        artwork_status=artwork_status,
        thumbnail_url=thumbnail_url or "",
        attempted_at=utc_now(),
    )


# ── Main run ─────────────────────────────────────────────────────────────────

def run(csv_path: Path, settings: RunSettings, output_dir: Optional[Path] = None) -> int:
    csv_path = csv_path.resolve()
    if not csv_path.exists():
        print(f"{SYM_FAIL} CSV not found: {csv_path}", file=sys.stderr)
        return 1

    if settings.cookies_file is not None:
        settings.cookies_file = settings.cookies_file.resolve()
        if not settings.cookies_file.exists():
            print(f"{SYM_FAIL} Cookies file not found: {settings.cookies_file}", file=sys.stderr)
            return 1

    if not settings.cookies_from_browser and settings.cookies_file is None:
        log(f"{SYM_WARN} No SoundCloud cookies provided — downloads will be 128kbps MP3 (free tier).")
        log(f"  For Go+ quality (256kbps AAC / original), add: --cookies-from-browser chrome")

    import csv as csv_mod
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv_mod.DictReader(f)
        if reader.fieldnames is None:
            print(f"{SYM_FAIL} CSV appears empty or invalid.", file=sys.stderr)
            return 1
        fieldnames = list(reader.fieldnames)
        rows = [dict(r) for r in reader]

    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        print(f"{SYM_FAIL} Missing required CSV columns: {', '.join(missing)}", file=sys.stderr)
        return 1

    fieldnames = ensure_tracking_columns(fieldnames)
    for row in rows:
        for col in fieldnames:
            row.setdefault(col, "")
    ensure_row_ids(rows)
    ensure_row_keys(rows)
    write_csv(csv_path, fieldnames, rows)

    output_dir = (output_dir or (csv_path.parent / playlist_stem(csv_path))).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    row_order = _processing_order(rows, settings.id_order)

    # Build the queue of rows that actually need work.
    pending: List[int] = []
    auto_skipped = 0
    for row_index in row_order:
        if settings.limit and len(pending) >= settings.limit:
            break
        row = rows[row_index]
        status = (row.get("download_status") or "").strip().lower()

        # Promote transient errors to retry so they're attempted again next run.
        # Only "unavailable" (deleted/private track) and "format" are permanent skips.
        _RETRYABLE = {"rate_limit", "auth", "network", "unknown"}
        if status == STATUS_ERROR:
            category = classify_download_error(row.get("error_message") or "")
            if category in _RETRYABLE:
                row["download_status"] = STATUS_RETRY
                status = STATUS_RETRY

        if status == STATUS_ERROR and not settings.force_redownload:
            auto_skipped += 1
            continue

        if _should_skip(row, settings.force_redownload):
            auto_skipped += 1
            continue

        pending.append(row_index)

    total = len(pending)
    if settings.cookies_from_browser:
        auth_str = f"Go+ via browser cookies ({settings.cookies_from_browser})"
    elif settings.cookies_file:
        auth_str = f"Go+ via cookies file"
    else:
        auth_str = f"{SYM_WARN} free tier — no cookies (128kbps MP3)"
    log_divider()
    log(f"cratedigg  {SYM_ARROW}  {csv_path.name}")
    log(f"  {total} tracks to process  |  {auto_skipped} already complete")
    log(f"  quality: {auth_str}")
    fmt_str = "native (best quality)" if not settings.prefer_mp3 else "mp3 320kbps"
    log(f"  format: {fmt_str}  |  workers: {settings.workers}")
    log_divider()

    counters: Dict[str, int] = dict(
        resolved=0, downloaded=0, skipped=auto_skipped,
        unresolved=0, errors=0, rate_limited=0
    )
    failures: List[FailedRow] = []
    rate_limited_flag = False
    completed = 0

    def flush_csv() -> None:
        ensure_all_columns(rows, fieldnames)
        write_csv(csv_path, fieldnames, rows)

    original_sigint = signal.getsignal(signal.SIGINT)
    executor_ref: List = [None]  # lets the SIGINT handler reach the executor

    def _sigint_handler(sig, frame):
        log(f"\n{SYM_WARN} Interrupted — flushing progress and stopping workers...")
        flush_csv()
        ex = executor_ref[0]
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)
        signal.signal(signal.SIGINT, original_sigint)
        os._exit(1)

    signal.signal(signal.SIGINT, _sigint_handler)

    with ThreadPoolExecutor(max_workers=settings.workers) as executor:
        executor_ref[0] = executor
        active: Dict[Future, int] = {}
        seq = 0
        pending_iter = iter(pending)

        def _submit_next() -> bool:
            """Submit the next pending row. Returns False when queue is exhausted."""
            nonlocal seq
            if rate_limited_flag:
                return False
            try:
                row_index = next(pending_iter)
            except StopIteration:
                return False
            seq += 1
            tag = _tag(seq, total)
            future = executor.submit(
                _worker, row_index, dict(rows[row_index]), output_dir, settings, tag
            )
            active[future] = (row_index, tag)
            return True

        # Fill up to max_workers initially.
        for _ in range(settings.workers):
            if not _submit_next():
                break

        while active:
            # as_completed on a snapshot; process one result then refill.
            done = next(iter(as_completed(list(active))))
            ri, tag = active.pop(done)
            row = rows[ri]
            track  = (row.get("Track Name") or "?").strip()
            artist = first_artist(row.get("Artist Name(s)") or "?")
            rid    = _row_id_value(row) or (ri + 1)

            result: Optional[RowResult] = None
            try:
                result = done.result()
            except RateLimitError as exc:
                if settings.yt_fallback:
                    label = f"{artist} - {track}"
                    dur_str = (row.get("Track Duration (ms)") or "").strip()
                    expected_s = int(dur_str) // 1000 if dur_str.isdigit() else 0
                    log(f"{tag} {SYM_WARN} SC rate limited — trying YouTube  [{label}]")
                    result = _yt_fallback(
                        ri, rid, row, output_dir, settings,
                        tag, label, expected_s, artist, track,
                    )
                else:
                    rate_limited_flag = True
                    err = format_error_for_display(str(exc))
                    log(f"  {SYM_WARN} Rate limited — stopping new submissions. Marked for retry.\n    {err}")
                    row["download_status"] = STATUS_RETRY
                    row["attempted_at"]    = utc_now()
                    row["error_message"]   = str(exc).strip()[:400]
                    counters["rate_limited"] += 1
                    counters["errors"] += 1
                    failures.append(FailedRow(rid, artist, track, STATUS_RETRY,
                                              "rate limited — rerun later"))
                    completed += 1
                    flush_csv()
                    continue
            except Exception as exc:
                err = format_error_for_display(str(exc))
                row["download_status"] = STATUS_ERROR
                row["attempted_at"]    = utc_now()
                row["error_message"]   = str(exc).strip()[:400]
                counters["errors"] += 1
                failures.append(FailedRow(rid, artist, track, STATUS_ERROR, err))
                completed += 1

            if result is not None:
                _apply_result(row, result)
                if result.status == STATUS_DOWNLOADED:
                    counters["downloaded"] += 1
                elif result.status == STATUS_RESOLVED:
                    counters["resolved"] += 1
                elif result.status == STATUS_UNRESOLVED:
                    counters["unresolved"] += 1
                    failures.append(FailedRow(
                        rid, artist, track, STATUS_UNRESOLVED,
                        f"no SoundCloud match within {settings.duration_tolerance}s",
                    ))
                elif result.status == STATUS_ERROR:
                    counters["errors"] += 1
                    display_err = format_error_for_display(result.error_message)
                    failures.append(FailedRow(rid, artist, track, STATUS_ERROR, display_err))
                completed += 1

            if completed % FLUSH_INTERVAL == 0:
                flush_csv()

            # Keep the pool full while there's work remaining.
            _submit_next()

    flush_csv()
    signal.signal(signal.SIGINT, original_sigint)

    # ── Summary ──────────────────────────────────────────────────────────────
    log_divider()
    log("Run complete")
    log(f"  {SYM_OK} Downloaded:   {counters['downloaded']}")
    log(f"  {SYM_ARROW} Resolved:     {counters['resolved']}  (match saved, not yet downloaded)")
    log(f"  {SYM_SKIP} Skipped:      {counters['skipped']}  (already complete)")
    log(f"  {SYM_MISS} Unresolved:   {counters['unresolved']}  (no SoundCloud match found)")
    log(f"  {SYM_FAIL} Errors:       {counters['errors']}")
    if rate_limited_flag:
        log(f"  {SYM_WARN} Rate limited: rerun to retry marked rows")

    if failures:
        log_divider()
        log("Failed tracks:")
        for f in failures:
            sym = SYM_MISS if f.status == STATUS_UNRESOLVED else SYM_FAIL
            log(f"  {sym} [{f.row_id:04d}] {f.artist} - {f.track}")
            log(f"         {SYM_ARROW} {f.reason}")

    log_divider()
    log(f"  CSV: {csv_path}")
    log(f"  Out: {output_dir}")
    log_divider()
    return 0

