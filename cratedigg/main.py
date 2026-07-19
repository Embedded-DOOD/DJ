#!/usr/bin/env python3
"""cratedigg -- source Spotify playlists from SoundCloud.

Usage:
    python main.py input/my_playlist.csv
    python main.py --csv-folder input/
    python main.py --sc-playlist "https://soundcloud.com/user/sets/my-set"
    python main.py input/my_playlist.csv --force-redownload
    python main.py input/my_playlist.csv --resolve-only

Config file (cratedigg.cfg in the same directory as main.py):
    [defaults]
    output_dir = D:\\
    cookies_file = sc_cookies.txt
"""

from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path

from cratedigg.core.csv_work_state import prepare_work_csv, prepare_sc_playlist_csv
from cratedigg.core.downloader import RunSettings, run
from cratedigg.core.sc_interface import extract_sc_playlist

_CFG_FILE  = Path(__file__).parent / "cratedigg.cfg"
_WORK_DIR  = Path(__file__).parent / "work"
_INPUT_DIR = Path(__file__).parent / "input"
_OUT_DIR   = Path(__file__).parent / "output"


def _load_cfg_defaults() -> dict:
    if not _CFG_FILE.exists():
        return {}
    cfg = configparser.ConfigParser()
    cfg.read(_CFG_FILE, encoding="utf-8")
    return dict(cfg["defaults"]) if cfg.has_section("defaults") else {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Spotify playlists sourced from SoundCloud.",
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("csv", nargs="?", type=Path,
                             help="Single Exportify CSV file.")
    input_group.add_argument("--csv-folder", type=Path, metavar="DIR",
                             help="Folder of Exportify CSV files (processes all).")
    input_group.add_argument("--sc-playlist", metavar="URL",
                             help="SoundCloud playlist, set, or user URL.")

    parser.add_argument("--output-dir", type=Path, default=_OUT_DIR, metavar="DIR",
                        help="Root folder for downloaded audio (default: ./output).")
    parser.add_argument("--cookies-file", type=Path, default=None, metavar="FILE",
                        help="Netscape cookies.txt for SoundCloud Go+ auth.")
    parser.add_argument("--resolve-only", action="store_true",
                        help="Find SoundCloud matches without downloading.")
    parser.add_argument("--force-redownload", action="store_true",
                        help="Re-download tracks already marked complete.")

    return parser


def make_settings(args: argparse.Namespace) -> RunSettings:
    return RunSettings(
        download_enabled=not args.resolve_only,
        force_redownload=args.force_redownload,
        cookies_file=args.cookies_file,
    )


def handle_sc_playlist(args: argparse.Namespace, settings: RunSettings) -> int:
    import re
    from cratedigg.core.utils import SYM_WARN
    url = args.sc_playlist.strip()

    if settings.cookies_file is None:
        print(f"{SYM_WARN} No SoundCloud cookies provided -- downloads will be 128kbps MP3 (free tier).")
        print(f"  Add 'cookies_file = sc_cookies.txt' to cratedigg.cfg for Go+ quality.")

    slug = re.sub(r"https?://soundcloud\.com/", "", url)
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug).strip("_")
    playlist_name = slug[:80] or "sc_playlist"

    _WORK_DIR.mkdir(parents=True, exist_ok=True)
    output_dir = args.output_dir.resolve() / playlist_name
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Extracting SoundCloud playlist: {url}")
    try:
        tracks = extract_sc_playlist(url, cookies_file=settings.cookies_file)
    except Exception as exc:
        print(f"Failed to extract playlist: {exc}", file=sys.stderr)
        return 1

    if not tracks:
        print("No tracks found at that URL.", file=sys.stderr)
        return 1

    print(f"Found {len(tracks)} tracks.")
    work_csv = prepare_sc_playlist_csv(playlist_name, _WORK_DIR, tracks)
    return run(work_csv, settings, output_dir)


def main() -> int:
    cfg = _load_cfg_defaults()
    parser = build_parser()

    overrides = {}
    if "output_dir" in cfg:
        overrides["output_dir"] = Path(cfg["output_dir"])
    if "cookies_file" in cfg:
        overrides["cookies_file"] = Path(cfg["cookies_file"])
    if overrides:
        parser.set_defaults(**overrides)

    args = parser.parse_args()
    settings = make_settings(args)

    _INPUT_DIR.mkdir(parents=True, exist_ok=True)
    _WORK_DIR.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.sc_playlist:
        return handle_sc_playlist(args, settings)

    if args.csv_folder:
        folder = args.csv_folder.resolve()
    elif args.csv is None:
        folder = _INPUT_DIR.resolve()
    else:
        folder = None

    if folder is not None:
        if not folder.is_dir():
            print(f"Folder not found: {folder}", file=sys.stderr)
            sys.exit(1)
        csv_files = sorted(
            p for p in folder.glob("*.csv")
            if not p.stem.lower().endswith("_work")
        )
        if not csv_files:
            print(f"No CSV files found in {folder}", file=sys.stderr)
            sys.exit(1)
    else:
        csv_files = [args.csv]

    for i, source_csv in enumerate(csv_files, 1):
        if len(csv_files) > 1:
            print(f"\n[{i}/{len(csv_files)}] {source_csv.name}")
        work_csv = prepare_work_csv(source_csv, _WORK_DIR)
        output_dir = args.output_dir.resolve() / source_csv.stem
        code = run(work_csv, settings, output_dir)
        if code != 0:
            return code

    return 0


if __name__ == "__main__":
    sys.exit(main())
