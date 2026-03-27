# NaviCraft

AI-powered playlist generator for [Navidrome](https://www.navidrome.org/). Describe the vibe you want, get a playlist built from your own music library.

## How It Works

```
1. SCAN
   Walk /music directory -> read tags with mutagen -> SQLite index
   (artist, album, title, genre, year, BPM, mood, duration, ...)

2. ENRICH (background)
   Query Last.fm + MusicBrainz for each track -> popularity scores (0-100)
   Higher scores = well-known, beloved tracks

3. GENERATE (two-pass AI)
   Pass 1: prompt + library summary -> structured filters (genres, era, mood, tempo, exclusions)
   SQLite query narrows to ~500 candidates, biased by popularity, capped per artist
   Pass 2: prompt + candidate list -> AI picks & orders the final playlist

4. CREATE PLAYLIST
   Match songs to Navidrome IDs -> Subsonic createPlaylist
   Or export as .m3u file for any music player
```

**Why two passes?** A 30k song library won't fit in a single AI prompt. Pass 1 uses a compact library summary to identify *what* to look for. SQLite narrows to ~500 candidates. Pass 2 gets full metadata for those candidates and selects the final playlist with good flow and variety.

## Features

- **Natural language prompts** — "Upbeat indie rock for a summer road trip" or "Jazz but NOT smooth jazz"
- **Popularity-aware** — Uses Last.fm listener data and MusicBrainz ratings so playlists favor well-known tracks over deep cuts
- **Negative filters** — "NOT", "no", "without" in prompts automatically exclude matching genres, artists, or keywords
- **Artist diversity** — Candidates are capped per artist so one artist doesn't dominate the playlist
- **Real-time progress** — SSE streaming shows each phase as it happens with elapsed time
- **Playlist history** — Browse and reuse prompts from past generations, or "More like this" to create variations
- **Multiple AI providers** — Claude (Anthropic) or Gemini (Google), switchable via config
- **Rich metadata** — Scans BPM, mood, composer, label directly from files (richer than Subsonic API)
- **Export options** — Save to Navidrome or download as .m3u

## Quick Start (Docker)

```bash
git clone https://github.com/chonzytron/navicraft.git
cd navicraft
cp .env.example .env
# Edit .env with your settings (see Configuration below)
docker compose up -d --build
```

Open `http://localhost:8085`

The first scan indexes your full library (may take a few minutes for 30k+ songs). Subsequent scans are incremental and fast. Popularity enrichment runs in the background every 10 minutes.

## Unraid Deployment

See [unraid/README.md](unraid/README.md) for detailed instructions. Two options:

### Option A: Unraid User Script (recommended)

1. Install **Nerd Tools** from Community Apps (for git)
2. Go to **Settings > User Scripts > Add New Script**
3. Paste the contents of `unraid/deploy-navicraft.sh`
4. Edit the top of the script — set your music path, port, and credentials
5. Click **Run Script**

### Option B: Docker Template

1. Copy `unraid/my-navicraft.xml` to `/boot/config/plugins/dockerMan/templates-user/`
2. Go to **Docker > Add Container**, select NaviCraft from the template dropdown
3. Fill in your settings and click **Apply**

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_DIR` | `/music` | Music directory inside container |
| `NAVIDROME_URL` | `http://localhost:4533` | Navidrome server URL |
| `NAVIDROME_USER` | `admin` | Navidrome username |
| `NAVIDROME_PASSWORD` | | Navidrome password |
| `AI_PROVIDER` | `claude` | `claude` or `gemini` |
| `CLAUDE_API_KEY` | | Anthropic API key |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Claude model |
| `GEMINI_API_KEY` | | Google AI key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model |
| `LASTFM_API_KEY` | | Last.fm API key ([get one free](https://www.last.fm/api/account/create)) — improves popularity scoring |
| `SCAN_INTERVAL_HOURS` | `6` | Auto-scan interval |
| `SCAN_EXTENSIONS` | `.mp3,.flac,.ogg,...` | File types to scan |
| `MAX_CANDIDATES` | `500` | Max songs sent to AI pass 2 |
| `DB_PATH` | `/data/navicraft.db` | SQLite database path |

### Docker Compose variables (host-side)

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_PATH` | `/mnt/user/media/music` | Host path to music library (mounted read-only) |
| `APPDATA_PATH` | `./data` | Host path for persistent data (SQLite DB) |
| `NAVICRAFT_PORT` | `8085` | Host port to expose |

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
│   ├── main.py          # FastAPI routes, SSE streaming, rate limiting
│   ├── config.py        # Environment variable config
│   ├── database.py      # SQLite schema, queries, migrations
│   ├── scanner.py       # mutagen-based file scanner
│   ├── ai_engine.py     # Two-pass AI (Claude / Gemini)
│   ├── navidrome.py     # Subsonic API client
│   ├── popularity.py    # Last.fm + MusicBrainz enrichment
│   ├── scheduler.py     # Background scan + enrichment jobs
│   └── requirements.txt
├── frontend/
│   └── index.html       # Single-file SPA (no build step)
├── unraid/
│   ├── deploy-navicraft.sh  # Unraid User Script
│   ├── my-navicraft.xml     # Unraid Docker template
│   └── README.md
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/navidrome/test` | Test Navidrome connection |
| GET | `/api/library/stats` | Library stats (counts, duration) |
| GET | `/api/library/genres` | All genres with counts |
| GET | `/api/library/search?q=` | Search tracks by text |
| POST | `/api/scan?full=false` | Trigger library scan |
| GET | `/api/scan/status` | Current scan progress |
| POST | `/api/generate` | Generate playlist (SSE stream, rate limited) |
| POST | `/api/playlists` | Save playlist to Navidrome |
| GET | `/api/playlists` | List Navidrome playlists |
| GET | `/api/playlists/history` | Local playlist generation history |
| DELETE | `/api/playlists/history/:id` | Delete from local history |
| DELETE | `/api/playlists/:id` | Delete from Navidrome |
| POST | `/api/popularity/re-enrich` | Reset and re-enrich all popularity data |
| GET | `/api/popularity/status` | Enrichment progress |
| POST | `/api/export/m3u` | Download playlist as .m3u file |

### Generate Request

```json
{
  "prompt": "Upbeat indie rock for a summer road trip",
  "max_songs": 30,
  "target_duration_min": 90,
  "auto_create": true
}
```

The response is an SSE stream with `progress` events (phase updates) followed by a `result` event with the final playlist.

## Tips

- **Tag your music well.** Genre and year are the most impactful tags for playlist quality. BPM and mood help too but are rarer.
- **Set up Last.fm.** A free API key dramatically improves playlist quality by letting NaviCraft know which tracks are popular vs. deep cuts.
- **Use negative filters.** "Jazz but NOT smooth jazz" or "Electronic without EDM" works — the AI extracts exclusions and applies them to the SQL query.
- **Claude vs Gemini:** Claude tends to produce more thoughtful, ordered playlists. Gemini is faster. Both work well.
- **Large libraries (50k+):** The two-pass strategy handles this fine. If you notice the AI missing songs, increase `MAX_CANDIDATES` (costs more tokens).
- **Navidrome ID sync:** NaviCraft matches songs to Navidrome by file path. If paths differ (e.g. symlinks), it falls back to artist+title matching.

## License

MIT
