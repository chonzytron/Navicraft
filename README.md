# NaviCraft

AI-powered playlist generator for [Navidrome](https://www.navidrome.org/). Describe the vibe you want, get a playlist built from your own music library.

## How It Works

```
1. SCAN
   Walk /music directory → read tags with mutagen → SQLite index
   (artist, album, title, genre, year, BPM, mood, duration, ...)
   Click the ♪ logo at any time to trigger a manual rescan.

2. ENRICH (background)
   Query Spotify + Last.fm + MusicBrainz for each track → popularity scores (0–100)
   Higher scores = well-known, beloved tracks
   Runs every 2 minutes; continues until all tracks have data from all available sources.
   Per-source progress tracked: spotify_checked_at / lastfm_checked_at per track.
   Confirmed-absent tracks (API responded, no match) are skipped for 24h before retry.

3. GENERATE (two-pass AI)
   Pass 1: prompt + library summary → structured filters (genres, era, mood, tempo, exclusions)
   SQLite query narrows to ~500 candidates, biased by popularity, capped per artist (max 15)
   Pass 2: prompt + candidate list → AI picks & orders the final playlist

4. CREATE PLAYLIST
   Match songs to Navidrome IDs → Subsonic createPlaylist
   Or export as .m3u file for any music player
```

**Why two passes?** A 30k song library won't fit in a single AI prompt. Pass 1 uses a compact library summary to identify *what* to look for. SQLite narrows to ~500 candidates. Pass 2 gets full metadata for those candidates and selects the final playlist with good flow and variety.

## Features

- **Natural language prompts** — "Upbeat indie rock for a summer road trip" or "Jazz but NOT smooth jazz"
- **Popularity-aware** — Uses Spotify streaming data, Last.fm listener counts, and MusicBrainz ratings so playlists favour well-known tracks over deep cuts
- **Negative filters** — "NOT", "no", "without" in prompts automatically exclude matching genres, artists, or keywords
- **Artist diversity** — Candidates are capped at 15 tracks per artist so one artist never dominates
- **Real-time progress** — SSE streaming shows each generation phase as it happens with elapsed time
- **Multiple AI providers** — Claude (Anthropic) or Gemini (Google); switch per-request in the UI when both keys are configured
- **Rich metadata** — Scans BPM, mood, composer, label directly from audio files (richer than the Subsonic API)
- **Export options** — Save directly to Navidrome or download as .m3u
- **Navidrome status** — Live connection indicator in the header; click to retest
- **Dual enrichment progress bars** — Last.fm (purple) and Spotify (green) progress bars visible while either source is still enriching

## Quick Start (Docker)

```bash
git clone https://github.com/chonzytron/navicraft.git
cd navicraft
cp .env.example .env
# Edit .env with your settings (see Configuration below)
docker compose up -d --build
```

Open `http://localhost:8085`

The first scan indexes your full library (a few minutes for large collections). Subsequent scans are incremental. Popularity enrichment runs in the background automatically.

## Unraid Deployment

See [unraid/README.md](unraid/README.md) for detailed instructions. Two options:

### Option A: Unraid User Script (recommended)

1. Install the **User Scripts** plugin from Community Applications
2. Go to **Settings > User Scripts > Add New Script**
3. Paste the contents of `unraid/deploy-navicraft.sh`
4. Edit the configuration block at the top — set your music path, Navidrome URL, and API keys
5. Click **Run Script**

The script always pulls the latest image, prunes old layers, and restarts the container cleanly.

### Option B: Docker Template

1. Copy `unraid/my-navicraft.xml` to `/boot/config/plugins/dockerMan/templates-user/`
2. Go to **Docker > Add Container**, select NaviCraft from the template dropdown
3. Fill in your settings and click **Apply**

## Configuration

### Container environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_DIR` | `/music` | Music directory inside the container |
| `NAVIDROME_URL` | `http://localhost:4533` | Navidrome server URL |
| `NAVIDROME_USER` | `admin` | Navidrome username |
| `NAVIDROME_PASSWORD` | — | Navidrome password |
| `AI_PROVIDER` | `claude` | Default AI provider: `claude` or `gemini` |
| `CLAUDE_API_KEY` | — | Anthropic API key (requires separate API billing) |
| `CLAUDE_MODEL` | `claude-3-5-sonnet-20241022` | Claude model identifier |
| `GEMINI_API_KEY` | — | Google AI API key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model identifier |
| `LASTFM_API_KEY` | — | Last.fm API key ([free](https://www.last.fm/api/account/create)) — improves popularity |
| `SPOTIFY_CLIENT_ID` | — | Spotify app client ID ([free](https://developer.spotify.com/dashboard)) — best popularity signal |
| `SPOTIFY_CLIENT_SECRET` | — | Spotify app client secret |
| `SCAN_INTERVAL_HOURS` | `6` | Background scan interval |
| `SCAN_EXTENSIONS` | `.mp3,.flac,.ogg,.opus,.m4a,.wma,.aac,.wav,.aiff,.ape,.wv,.mpc` | File types to index |
| `MAX_CANDIDATES` | `500` | Max songs passed to AI Pass 2 |
| `DB_PATH` | `/data/navicraft.db` | SQLite database path |

### Docker Compose host variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_PATH` | `/mnt/user/media/music` | Host path mounted read-only at `/music` |
| `APPDATA_PATH` | `./data` | Host path for persistent data (SQLite DB) |
| `NAVICRAFT_PORT` | `8085` | Host port |

> **Note on Claude API:** A Claude.ai subscription (Pro/Team) does **not** include API access. The Anthropic API requires separate credits at [console.anthropic.com](https://console.anthropic.com). Gemini offers a generous free tier and works well as an alternative.

## Popularity Enrichment

NaviCraft scores each track 0–100 using up to four sources, blended by confidence:

| Source | Signal | Notes |
|--------|--------|-------|
| **Spotify** | Real streaming popularity (0–100) | Best signal; requires free app credentials |
| **Last.fm** | Listener count + scrobble ratio | Good; free API key |
| **MusicBrainz** | Community ratings + release count | Fallback; no key needed |
| **Track position** | Album position heuristic | +5 for tracks 1–2, +3 for 3–4 |

MusicBrainz is only queried when Spotify and Last.fm lack signal, keeping enrichment fast.

**Spotify rate limiting:** The `Retry-After` header from 429 responses is honored exactly (server-side blocks can be hours long). The cooldown timestamp is persisted to the SQLite `settings` table so a container restart doesn't trigger a wasted attempt during an active block. Spotify requests run at 0.5 req/s (2 req/s) to stay well within the ~250 req/30s limit.

**Batch lookups:** Once a track's Spotify ID is stored from the initial search, future top-up passes use `GET /v1/tracks?ids=...` with up to 50 IDs per request — ~50× more efficient than individual searches.

**Not-found backoff:** When an API definitively returns "not found" (200 OK, empty results), the track is flagged with a `checked_at` timestamp and skipped for 24 hours. Transient errors (timeouts, 429s, network failures) are not flagged and retry normally on the next cycle.

## Metadata Extracted

NaviCraft reads tags directly from your files using `mutagen`:

- Title, Artist, Album Artist, Album
- Genre, Year, Track / Disc number
- Duration, BPM, Sample rate, Bitrate
- Composer, Mood, Comment, Label
- File format, path, size

This is richer than what the Subsonic API exposes — especially BPM and mood, which help the AI make better selections.

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
│   ├── popularity.py    # Spotify + Last.fm + MusicBrainz enrichment
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
| GET | `/api/ai/providers` | List configured AI providers |
| GET | `/api/navidrome/test` | Test Navidrome connection |
| GET | `/api/library/stats` | Library stats (counts, duration, genres) |
| GET | `/api/library/genres` | All genres with counts |
| GET | `/api/library/search?q=` | Search tracks by text |
| POST | `/api/scan?full=false` | Trigger library scan (incremental or full) |
| GET | `/api/scan/status` | Current scan progress |
| POST | `/api/generate` | Generate playlist — SSE stream, 10s rate limit |
| POST | `/api/playlists` | Save playlist to Navidrome |
| GET | `/api/playlists` | List Navidrome playlists |
| DELETE | `/api/playlists/:id` | Delete from Navidrome |
| POST | `/api/popularity/enrich` | Manually trigger an enrichment batch |
| POST | `/api/popularity/re-enrich` | Reset and re-enrich all popularity data |
| GET | `/api/popularity/status` | Per-source enrichment progress (total, enriched/remaining/percent for overall, Spotify, and Last.fm) |
| POST | `/api/export/m3u` | Download playlist as .m3u file |

### Generate request

```json
{
  "prompt": "Upbeat indie rock for a summer road trip",
  "max_songs": 30,
  "target_duration_min": 90,
  "auto_create": false,
  "provider": "gemini"
}
```

The response is an SSE stream: `progress` events for each phase, then a `result` event with the full playlist, or an `error` event with the actual error message.

## Tips

- **Tag your music well.** Genre and year are the most impactful tags for playlist quality. BPM and mood help too but are rarer.
- **Set up Spotify + Last.fm.** Both offer free credentials and together give the best popularity signal.
- **Use negative filters.** "Jazz but NOT smooth jazz" or "Electronic without EDM" works — the AI extracts exclusions and applies them at the SQL query stage.
- **Claude vs Gemini:** Claude tends to produce more thoughtful, ordered playlists. Gemini is faster and has a generous free tier. Both can be active simultaneously and switched per-request in the UI.
- **Large libraries (50k+):** The two-pass strategy handles this well. If the AI misses songs you'd expect, increase `MAX_CANDIDATES` (uses more tokens per request).
- **Navidrome ID sync:** NaviCraft matches songs to Navidrome by file path. If paths differ (e.g. symlinks), it falls back to artist + title matching.
- **Manual rescan:** Click the ♪ logo mark in the top-left to trigger an incremental rescan at any time.

## License

MIT
