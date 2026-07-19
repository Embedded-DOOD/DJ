#!/usr/bin/env python3
"""cratedigg — source Spotify playlists from SoundCloud.

Takes an Exportify CSV and downloads each track from SoundCloud
instead of YouTube Music, preserving native audio quality (Go+).

Usage:
    python main.py playlist.csv
    python main.py playlist.csv --workers 4 --mp3
    python main.py playlist.csv --cookies-from-browser chrome
    python main.py ./exportify_csvs/ --csv-folder
    python main.py playlist.csv --resolve-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cratedigg.core.csv_work_state import prepare_work_csv, prepare_sc_playlist_csv
from cratedigg.core.downloader import RunSettings, run
from cratedigg.core.sc_interface import extract_sc_playlist


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Spotify playlists sourced from SoundCloud.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Input — mutually exclusive: Spotify CSV, CSV folder, or SC playlist URL
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("csv", nargs="?", type=Path, help="Single Exportify CSV file.")
    input_group.add_argument("--csv-folder", type=Path, metavar="DIR",
                             help="Folder of Exportify CSV files (processes all).")
    input_group.add_argument("--sc-playlist", metavar="URL",
                             help="SoundCloud playlist, set, or user URL to download directly.")

    # Behaviour
    parser.add_argument("--resolve-only", action="store_true",
                        help="Find SoundCloud matches and save to work CSV, but skip download.")
    parser.add_argument("--force-redownload", action="store_true",
                        help="Re-download rows already marked as downloaded.")
    parser.add_argument("--mp3", action="store_true",
                        help="Transcode to MP3 320kbps (default: preserve native format).")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Stop after N rows (0 = all).")
    parser.add_argument("--workers", type=int, default=3, metavar="N",
                        help="Parallel download workers (default: 3).")

    # Matching
    parser.add_argument("--duration-tolerance", type=int, default=10, metavar="SEC",
                        help="Max duration difference in seconds for a match (default: 10).")
    parser.add_argument("--search-results", type=int, default=6, metavar="N",
                        help="SoundCloud candidates to score per track (default: 6).")
    parser.add_argument("--id-order", choices=["default", "priority", "ascending", "descending"],
                        default="priority", help="Row processing order (default: priority).")

    # Rate limiting
    parser.add_argument("--sleep-requests", type=float, default=1.1, metavar="SEC",
                        help="Delay between yt-dlp requests (default: 1.1).")
    parser.add_argument("--limit-rate", default="", metavar="RATE",
                        help="Download rate cap e.g. 4M.")
    parser.add_argument("--throttled-rate", default="", metavar="RATE",
                        help="yt-dlp throttled rate fallback e.g. 50K.")
    parser.add_argument("--sleep-interval", type=float, default=0.0)
    parser.add_argument("--max-sleep-interval", type=float, default=0.0)

    # SoundCloud auth (Go+ quality)
    parser.add_argument("--cookies-from-browser", default="", metavar="BROWSER",
                        help="Browser to pull SoundCloud cookies from (e.g. chrome, firefox, edge). "
                             "Required for Go+ quality.")
    parser.add_argument("--cookies-file", type=Path, default=None, metavar="FILE",
                        help="Netscape cookies.txt file for SoundCloud auth.")

    return parser


def make_settings(args: argparse.Namespace) -> RunSettings:
    return RunSettings(
        duration_tolerance=args.duration_tolerance,
        search_results=args.search_results,
        download_enabled=not args.resolve_only,
        force_redownload=args.force_redownload,
        limit=args.limit,
        workers=args.workers,
        prefer_mp3=args.mp3,
        sleep_requests=args.sleep_requests,
        limit_rate=args.limit_rate,
        throttled_rate=args.throttled_rate,
        sleep_interval=args.sleep_interval,
        max_sleep_interval=args.max_sleep_interval,
        id_order=args.id_order,
        cookies_from_browser=args.cookies_from_browser,
        cookies_file=args.cookies_file,
    )


def collect_csv_files(args: argparse.Namespace) -> list[Path]:
    if args.csv_folder:
        folder = args.csv_folder.resolve()
        if not folder.is_dir():
            print(f"Folder not found: {folder}", file=sys.stderr)
            sys.exit(1)
        files = sorted(
            p for p in folder.glob("*.csv")
            if not p.stem.lower().endswith("_work")
        )
        if not files:
            print(f"No CSV files found in {folder}", file=sys.stderr)
            sys.exit(1)
        return files
    return [args.csv]


def handle_sc_playlist(args: argparse.Namespace, settings: RunSettings) -> int:
    """Extract a SoundCloud playlist and download it directly."""
    import re
    url = args.sc_playlist.strip()

    # Derive a safe folder/CSV name from the URL.
    slug = re.sub(r"https?://soundcloud\.com/", "", url)
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug).strip("_")
    playlist_name = slug[:80] or "sc_playlist"

    output_dir = Path.cwd() / playlist_name
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Extracting SoundCloud playlist: {url}")
    try:
        tracks = extract_sc_playlist(
            url,
            cookies_from_browser=settings.cookies_from_browser,
            cookies_file=settings.cookies_file,
            sleep_requests=settings.sleep_requests,
        )
    except Exception as exc:
        print(f"Failed to extract playlist: {exc}", file=sys.stderr)
        return 1

    if not tracks:
        print("No tracks found at that URL.", file=sys.stderr)
        return 1

    print(f"Found {len(tracks)} tracks.")
    work_csv = prepare_sc_playlist_csv(playlist_name, output_dir, tracks)
    return run(work_csv, settings)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    settings = make_settings(args)

    if args.sc_playlist:
        return handle_sc_playlist(args, settings)

    csv_files = collect_csv_files(args)

    for i, source_csv in enumerate(csv_files, 1):
        if len(csv_files) > 1:
            print(f"\n[{i}/{len(csv_files)}] {source_csv.name}")
        work_csv = prepare_work_csv(source_csv)
        code = run(work_csv, settings)
        if code != 0:
            return code

    return 0


if __name__ == "__main__":
    sys.exit(main())
