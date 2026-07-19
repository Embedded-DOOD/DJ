# cratedigg

> Source your Spotify playlists from SoundCloud — higher quality, your library, your files.

**cratedigg** is named after *crate digging* — the DJ practice of hunting through record crates for rare, high-quality tracks. The tool does the same thing digitally: takes your Spotify playlist and finds the best available version of each track on SoundCloud, preserving native audio quality instead of settling for a YouTube re-encode.

cratedigg takes an Exportify CSV (your Spotify playlist) and downloads each track from SoundCloud instead of YouTube Music. With SoundCloud Go+, you get 256kbps AAC or the artist's original upload. It can also download SoundCloud playlists and sets directly without any Spotify CSV at all.

> **SoundCloud Go+ is strongly recommended.** Without it, SoundCloud serves 128kbps MP3 — no better than a YouTube rip. Go+ unlocks 256kbps AAC and original-quality uploads. See [SoundCloud Go+](https://soundcloud.com/go).

---

## How It Works

Two input modes, one download engine:

```mermaid
flowchart TD
    subgraph mode1 ["Mode 1 — Spotify Playlist"]
        A1([Exportify CSV]) --> B1[Search SoundCloud\nby title + artist + duration]
        B1 --> C1[Score candidates\nduration · title · artist overlap]
        C1 --> D1{Match\nfound?}
        D1 -- Yes --> E1[Download best match]
        D1 -- No --> F1([unresolved])
    end

    subgraph mode2 ["Mode 2 — SoundCloud Playlist Direct"]
        A2([SoundCloud URL]) --> B2[Extract all tracks\nfrom playlist]
        B2 --> E2[Download directly\nno search needed]
    end

    E1 --> G[Embed metadata + cover art\ntitle · artist · album · ISRC · source URL]
    E2 --> G
    G --> H([Audio file\nnative AAC / MP3])
    G --> I([work CSV\nresumable state])
```

**Key behaviors:**
- **Native quality by default** — preserves the SoundCloud stream format (AAC with Go+). `--mp3` transcodes to 320kbps MP3 if you need it.
- **Resumable** — every row's state is written to a `_work.csv`. Interrupted runs pick up exactly where they left off.
- **Parallel workers** — configurable concurrent downloads (default: 3).
- **Source URL embedded** — the SoundCloud track link is written into the audio file's `WOAS` ID3 tag and the work CSV `source_url` column.

---

## Quick Start

**Prerequisites:** Python 3.10+, ffmpeg on PATH (see below), [SoundCloud Go+](https://soundcloud.com/go) subscription

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install ffmpeg (Windows — one-time)
Invoke-WebRequest -Uri "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile "$env:TEMP\ffmpeg.zip"
Expand-Archive "$env:TEMP\ffmpeg.zip" -DestinationPath "$env:TEMP\ffmpeg"
Copy-Item (Get-ChildItem "$env:TEMP\ffmpeg" -Recurse -Filter "ffmpeg.exe" | Select -First 1).FullName -Destination "."
Copy-Item (Get-ChildItem "$env:TEMP\ffmpeg" -Recurse -Filter "ffprobe.exe" | Select -First 1).FullName -Destination "."

# 3. Export your Spotify playlist (see below), then run
python main.py my_playlist.csv
```

---

## Getting Your Spotify CSV

cratedigg uses [Exportify](https://exportify.app) to get your Spotify playlist data.

1. Go to **[exportify.app](https://exportify.app)**
2. Click **Log in with Spotify** and authorize the app
3. Your playlists appear as a list — find the one you want
4. Click **Export** next to it → a `.csv` file downloads automatically
5. Place the `.csv` file in the `input/` folder — cratedigg will find it automatically

> **Tip:** You can export multiple playlists and put them all in `input/`, then run `python main.py --csv-folder input/` to process them all in one go.

---

### Spotify playlist (via Exportify CSV)

```powershell
# Drop CSV in input/, then run from the cratedigg/ directory:
python main.py input/my_playlist.csv

# Best quality — unlock Go+ streams via browser cookies
python main.py input/my_playlist.csv --cookies-from-browser chrome

# Force MP3 output instead of native format
python main.py input/my_playlist.csv --mp3 --cookies-from-browser chrome

# Process all CSVs in the input/ folder at once
python main.py --csv-folder input/

# Preview matches without downloading (useful first pass)
python main.py input/my_playlist.csv --resolve-only
```

### SoundCloud playlist direct

```powershell
# Download a SoundCloud set or playlist
python main.py --sc-playlist "https://soundcloud.com/user/sets/my-set"

# Download all tracks from a user's page
python main.py --sc-playlist "https://soundcloud.com/username" --cookies-from-browser chrome

# Download your SoundCloud likes
python main.py --sc-playlist "https://soundcloud.com/username/likes" --cookies-from-browser chrome
```

### Resume and retry

```powershell
# Rerun — already-downloaded rows are skipped automatically
python main.py input/my_playlist.csv

# Force re-download everything
python main.py input/my_playlist.csv --force-redownload
```

---

## CLI Reference

### Input (pick one)

| Flag | Description |
|---|---|
| `csv PATH` | Single Exportify CSV file (positional argument) |
| `--csv-folder DIR` | Process all CSV files in a folder |
| `--sc-playlist URL` | Download a SoundCloud playlist, set, or user page directly — no CSV needed |

---

### Directories

| Flag | Default | Description |
|---|---|---|
| `--output-dir DIR` | `./output` | Root folder for downloaded audio. Pass `D:\` to send directly to a USB drive. |
| `--work-dir DIR` | `./work` | Where resumable work-state CSVs are stored (auto-managed — don't edit) |
| `--input-dir DIR` | `./input` | Folder scanned when using `--csv-folder` without a path |

---

### Quality & Auth

| Flag | Default | Description |
|---|---|---|
| `--cookies-from-browser BROWSER` | — | Read your SoundCloud session from an installed browser: `chrome`, `firefox`, `edge`, `brave`. **Required for Go+ quality.** |
| `--cookies-file FILE` | — | Netscape-format `cookies.txt` alternative to browser extraction |
| `--mp3` | off | Transcode output to MP3 320kbps. Default preserves native SoundCloud format (AAC with Go+). |

---

### Download Behaviour

| Flag | Default | Description |
|---|---|---|
| `--workers N` | `3` | Number of parallel download threads |
| `--resolve-only` | off | Find and score SoundCloud matches, save to work CSV, skip actual download. Good for a first-pass review. |
| `--force-redownload` | off | Re-download rows already marked as complete |
| `--limit N` | `0` (all) | Stop after N rows — useful for testing |

---

### Matching

| Flag | Default | Description |
|---|---|---|
| `--duration-tolerance SEC` | `10` | Maximum allowed duration difference (seconds) between the Spotify track and a SoundCloud candidate. Increase if legitimate matches are being skipped. |
| `--search-results N` | `6` | Number of SoundCloud candidates to score per track. Higher values find more obscure matches but are slower. |
| `--id-order` | `priority` | Row processing order: `priority` (new first, then retries) · `ascending` (by row ID) · `descending` · `default` (CSV order) |

---

### Rate Limiting

| Flag | Default | Description |
|---|---|---|
| `--sleep-requests SEC` | `1.1` | Delay between yt-dlp requests to avoid SoundCloud rate limits |
| `--sleep-interval SEC` | `0.0` | Minimum sleep between downloads |
| `--max-sleep-interval SEC` | `0.0` | Maximum sleep between downloads (enables jitter when > 0) |
| `--limit-rate RATE` | — | Cap download bandwidth, e.g. `4M` for 4 MB/s |
| `--throttled-rate RATE` | — | yt-dlp throttled-rate fallback, e.g. `50K` |

---

## Output Structure

```
DJ/cratedigg/
├── input/
│   └── my_playlist.csv          ← drop Exportify CSVs here
│
├── output/
│   └── my_playlist/             ← audio files, one folder per playlist
│       ├── 0001 - Artist - Track.m4a
│       ├── 0002 - Artist - Track.m4a
│       └── ...
│
└── work/
    └── my_playlist_work.csv     ← auto-managed state (don't edit)

# SC playlist direct mode:
└── work/
    └── username_sets_my-set_work.csv
└── output/
    └── username_sets_my-set/
        └── 0001 - Artist - Track.m4a
```

The `_work.csv` file is your resumable state. It tracks:

| Column | What it stores |
|---|---|
| `download_status` | Current row state (see below) |
| `source_url` | SoundCloud track URL used for the download |
| `matched_title` | Title of the SoundCloud track that was matched |
| `duration_delta_s` | Seconds difference between Spotify and SoundCloud duration |
| `output_file` | Absolute path to the downloaded file |
| `output_format` | Container format: `m4a`, `mp3`, `opus`, etc. |
| `error_message` | Human-readable failure reason if something went wrong |

---

## Row Status Reference

| Status | Symbol | Meaning | Action |
|---|---|---|---|
| `downloaded` | ✓ | File on disk, complete | — |
| `resolved` | → | SC match saved, download pending | Rerun without `--resolve-only` |
| `unresolved` | ? | No SC match within duration tolerance | Try `--duration-tolerance 20` |
| `error` | ✗ | Permanent failure | Check `error_message` column |
| `retry` | ! | Hit rate limit | Rerun later — row is automatically retried |

---

## SoundCloud Go+ Quality

With Go+, SoundCloud delivers **256kbps AAC** or the **original uploaded file** on tracks where the artist enabled it — significantly better than the 128kbps MP3 you get without a subscription.

To unlock Go+ streams, pass your browser's SoundCloud session cookies:

```powershell
python main.py my_playlist.csv --cookies-from-browser chrome
```

Your browser must be open and logged into SoundCloud with an active Go+ subscription. The `--cookies-from-browser` flag works with `chrome`, `firefox`, `edge`, and `brave`.

> **Note:** Even with Go+, not every track exposes an original-quality stream — it depends on what the artist uploaded. The tool always selects the best available format.

---

## Rekordbox Integration

1. Download your playlist with cratedigg
2. In Rekordbox: **File → Add Folder to Collection**
3. Point it at the playlist folder (e.g. `D:\my_playlist\`)
4. Rekordbox reads the embedded metadata (title, artist, album, BPM after analysis)

Native AAC (`.m4a`) and MP3 are both fully supported by Rekordbox.

