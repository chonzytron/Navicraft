# NaviCraft

AI-powered playlist generator for [Navidrome](https://www.navidrome.org/) and [Plex/Plexamp](https://www.plex.tv/). Describe the vibe you want, get a playlist built from your own music library.

## How It Works

```
1. SCAN
   Walk /music directory → read tags with mutagen → SQLite index
   (artist, album, title, genre, year, BPM, mood, duration, ...)
   Click the ♪ logo at any time to trigger a manual rescan.

2. ENRICH (background)
   Query Spotify + Last.fm + MusicBrainz for each track → popularity scores (0–100)
   Higher scores = well-known, beloved tracks
   Runs automatically every 2 minutes until all tracks are enriched.

3. GENERATE (two-pass AI)
   Pass 1: prompt + library summary → structured filters (genres, era, mood, tempo, exclusions)
   SQLite query narrows to ~500 candidates, biased by popularity, capped per artist (max 15)
   Pass 2: prompt + candidate list → AI picks & orders the final playlist

4. CREATE PLAYLIST
   Match songs to Navidrome IDs → Subsonic createPlaylist
   Or match to Plex ratingKeys → Plex HTTP API createPlaylist
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
- **Multiple media servers** — Save playlists to Navidrome, Plex/Plexamp, or both. Toggle between servers in the UI when both are configured.
- **Export options** — Save to your media server or download as .m3u
- **Server status** — Live connection indicators in the header for each configured server; click to retest

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
| `NAVIDROME_URL` | `http://localhost:4533` | Navidrome server URL (leave empty if using Plex only) |
| `NAVIDROME_USER` | `admin` | Navidrome username |
| `NAVIDROME_PASSWORD` | — | Navidrome password |
| `PLEX_URL` | — | Plex server URL (e.g. `http://localhost:32400`). Leave empty if using Navidrome only. |
| `PLEX_TOKEN` | — | Plex authentication token ([how to find](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
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

MusicBrainz is only queried when Spotify and Last.fm lack signal, keeping enrichment fast. Spotify backs off automatically on rate limits (10-minute cooldown after 3 consecutive 429s).

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
│   ├── plex.py          # Plex HTTP API client
│   ├── popularity.py    # Spotify + Last.fm + MusicBrainz enrichment
│   ├── scheduler.py     # Background scan + enrichment jobs
│   └── requirements.txt
├── frontend/
│   ├── index.html       # SPA markup (no build step)
│   └── assets/
│       ├── app.js       # Frontend logic
│       └── styles.css   # Styles
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
| GET | `/api/servers` | List configured media servers |
| GET | `/api/navidrome/test` | Test Navidrome connection |
| GET | `/api/plex/test` | Test Plex connection |
| GET | `/api/library/stats` | Library stats (counts, duration, genres) |
| GET | `/api/library/genres` | All genres with counts |
| GET | `/api/library/search?q=` | Search tracks by text |
| POST | `/api/scan?full=false` | Trigger library scan (incremental or full) |
| GET | `/api/scan/status` | Current scan progress |
| POST | `/api/generate` | Generate playlist — SSE stream, 10s rate limit |
| POST | `/api/playlists` | Save playlist to Navidrome or Plex |
| GET | `/api/playlists` | List playlists from active server |
| DELETE | `/api/playlists/:id` | Delete playlist from active server |
| POST | `/api/popularity/enrich` | Manually trigger an enrichment batch |
| POST | `/api/popularity/re-enrich` | Reset and re-enrich all popularity data |
| GET | `/api/popularity/status` | Enrichment progress (enriched/total/%) |
| POST | `/api/export/m3u` | Download playlist as .m3u file |

### Generate request

```json
{
  "prompt": "Upbeat indie rock for a summer road trip",
  "max_songs": 30,
  "target_duration_min": 90,
  "auto_create": false,
  "provider": "gemini",
  "server": "navidrome"
}
```

The response is an SSE stream: `progress` events for each phase, then a `result` event with the full playlist, or an `error` event with the actual error message.

## Tips

- **Tag your music well.** Genre and year are the most impactful tags for playlist quality. BPM and mood help too but are rarer.
- **Set up Spotify + Last.fm.** Both offer free credentials and together give the best popularity signal.
- **Use negative filters.** "Jazz but NOT smooth jazz" or "Electronic without EDM" works — the AI extracts exclusions and applies them at the SQL query stage.
- **Claude vs Gemini:** Claude tends to produce more thoughtful, ordered playlists. Gemini is faster and has a generous free tier. Both can be active simultaneously and switched per-request in the UI.
- **Large libraries (50k+):** The two-pass strategy handles this well. If the AI misses songs you'd expect, increase `MAX_CANDIDATES` (uses more tokens per request).
- **Media server ID sync:** NaviCraft matches songs to Navidrome/Plex by file path. If paths differ (e.g. symlinks), it falls back to artist + title matching. When both servers are configured, IDs are synced independently for each.
- **Manual rescan:** Click the ♪ logo mark in the top-left to trigger an incremental rescan at any time.

## License

MIT
