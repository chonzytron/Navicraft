# NaviCraft

AI-powered playlist generator for Navidrome. Describe the vibe you want, get a playlist built from your own music library.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│  1. SCAN                                                        │
│  Walk /music directory → read tags with mutagen → SQLite index  │
│  (artist, album, title, genre, year, BPM, mood, duration, ...) │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│  2. GENERATE (two-pass AI)                                      │
│                                                                 │
│  Pass 1 — Intent Extraction                                     │
│  Send prompt + library summary (genres, artists, years)         │
│  → AI returns structured filters (genres, era, mood, tempo)     │
│                                                                 │
│  SQLite Query                                                   │
│  Filter index with AI-generated criteria → ~500 candidates      │
│                                                                 │
│  Pass 2 — Song Selection                                        │
│  Send prompt + candidate list → AI picks & orders final songs   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│  3. CREATE PLAYLIST                                             │
│  Match selected songs to Navidrome IDs → Subsonic createPlaylist│
│  Playlist appears in all your Subsonic-compatible clients       │
│  Or export as .m3u file for use with any music player           │
└─────────────────────────────────────────────────────────────────┘
```

**Why two passes?** A 30k song library won't fit in a single AI prompt. Pass 1 uses a compact library summary to identify *what* to look for. SQLite narrows to ~500 candidates. Pass 2 gets full metadata for those candidates and selects the final playlist with good flow and variety.

## Quick Start (Unraid)

### Option A: Unraid User Script (recommended)

Automatically pulls from GitHub, builds, and starts. Also handles updates.

1. Install **Nerd Tools** from Community Apps (for git) if you don't have it
2. Go to **Settings → User Scripts → Add New Script**
3. Name it `navicraft-deploy`
4. Paste the contents of `scripts/unraid-deploy.sh`
5. Edit the top of the script — set your GitHub repo, music path, and port
6. Click **Run Script**
7. On first run, edit `/mnt/user/appdata/navicraft/.env` with your credentials
8. Run the script again to start with your config

To update: just run the script again. It pulls, rebuilds, and restarts.

### Option B: Manual

```bash
# Clone
cd /mnt/user/appdata
git clone https://github.com/YOUR_USERNAME/navicraft.git
cd navicraft

# Configure
cp .env.example .env
nano .env   # set your Navidrome URL, password, API key

# Set your music path and start
MUSIC_PATH=/mnt/user/media/music docker compose up -d --build
```

Open `http://your-unraid-ip:8085`

The first scan indexes your full library (may take a few minutes for 30k+ songs). Subsequent scans are incremental and fast.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_DIR` | `/music` | Music directory inside container |
| `NAVIDROME_URL` | `http://localhost:4533` | Navidrome server URL |
| `NAVIDROME_USER` | `admin` | Navidrome username |
| `NAVIDROME_PASSWORD` | — | Navidrome password |
| `AI_PROVIDER` | `claude` | `claude` or `gemini` |
| `CLAUDE_API_KEY` | — | Anthropic API key |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Claude model |
| `GEMINI_API_KEY` | — | Google AI key |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model |
| `SCAN_INTERVAL_HOURS` | `6` | Auto-scan interval |
| `SCAN_EXTENSIONS` | `.mp3,.flac,.ogg,...` | File types to scan |
| `MAX_CANDIDATES` | `500` | Max songs sent to AI pass 2 |
| `DB_PATH` | `/data/navicraft.db` | SQLite database path |

## Metadata Extracted

NaviCraft reads tags directly from your files using `mutagen`:

- Title, Artist, Album Artist, Album
- Genre, Year, Track/Disc number
- Duration, BPM, Sample rate, Bitrate
- Composer, Mood, Comment, Label
- File format, path, size

This is significantly richer than what the Subsonic API exposes — especially BPM and mood tags, which help the AI make better selections.

## Architecture

```
navicraft/
├── backend/
│   ├── main.py          # FastAPI routes + startup logic
│   ├── config.py        # Environment variable config
│   ├── database.py      # SQLite schema + queries
│   ├── scanner.py       # mutagen-based file scanner
│   ├── ai_engine.py     # Two-pass AI (Claude / Gemini)
│   ├── navidrome.py     # Subsonic API client
│   ├── scheduler.py     # APScheduler for periodic scans
│   └── requirements.txt
├── frontend/
│   └── index.html       # Single-file SPA (no build step)
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/navidrome/test` | Test Navidrome connection |
| GET | `/api/library/stats` | Library stats + genres + moods |
| GET | `/api/library/search?q=` | Search tracks by text |
| POST | `/api/scan?full=false` | Trigger library scan |
| GET | `/api/scan/status` | Current scan progress |
| POST | `/api/generate` | Generate playlist (two-pass AI) |
| POST | `/api/export/m3u` | Download playlist as .m3u file |
| POST | `/api/playlists` | Save playlist to Navidrome |
| GET | `/api/playlists` | List Navidrome playlists |
| DELETE | `/api/playlists/:id` | Delete a playlist |

### Generate Request

```json
{
  "prompt": "Upbeat indie rock for a summer road trip",
  "max_songs": 30,
  "target_duration_min": 90,
  "auto_create": true
}
```

## Tips

- **Tag your music well.** Genre and year are the most impactful tags for playlist quality. BPM and mood help too but are rarer.
- **Claude vs Gemini:** Claude tends to produce more thoughtful, ordered playlists. Gemini is faster and handles larger candidate lists. Both work well.
- **Large libraries (50k+):** The two-pass strategy handles this fine. If you notice the AI missing songs, increase `MAX_CANDIDATES` (costs more tokens).
- **Navidrome ID sync:** NaviCraft matches songs to Navidrome by file path. If paths differ (e.g. symlinks), it falls back to artist+title matching.

## License

MIT
