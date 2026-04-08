# NaviCraft

AI-powered playlist generator for [Navidrome](https://www.navidrome.org/) and [Plex/Plexamp](https://www.plex.tv/). Describe the vibe you want, get a playlist built from your own music library.

## How It Works

```
1. SCAN
   Walk /music directory → read tags with mutagen → SQLite index
   (artist, album, title, genre, year, BPM, mood, duration, ...)
   Click the ♪ logo at any time to trigger a manual rescan.

2. ENRICH (background)
   Query Deezer + Last.fm + MusicBrainz for each track → popularity scores (0–100)
   Higher scores = well-known, beloved tracks
   Runs automatically every 2 minutes until all tracks are enriched.

2b. MOOD/THEME TAG (optional, background)
    Essentia MTG-Jamendo model analyzes audio → mood tags (happy, sad, energetic, ...)
    + theme tags (film, party, nature, summer, ...).
    Supplemented with Last.fm user tags and MusicBrainz folksonomy tags.
    Configurable: process X tracks every Y hours. Enable in Settings.

3. GENERATE (two-pass AI)
   Pass 1: prompt + library summary → structured filters (genres, era, mood, tempo, exclusions)
   SQLite query narrows to ~500 candidates, biased by popularity, with proportional per-artist diversity cap
   Pass 2: prompt + candidate list → AI picks & orders the final playlist

4. CREATE PLAYLIST
   Match songs to Navidrome IDs → Subsonic createPlaylist
   Or match to Plex ratingKeys → Plex HTTP API createPlaylist
   Or export as .m3u file for any music player
```

**Why two passes?** A 30k song library won't fit in a single AI prompt. Pass 1 uses a compact library summary to identify *what* to look for. SQLite narrows to ~500 candidates. Pass 2 gets full metadata for those candidates and selects the final playlist with good flow and variety.

## Features

- **Natural language prompts** — "Upbeat indie rock for a summer road trip" or "Jazz but NOT smooth jazz"
- **Popularity-aware** — Uses Deezer track rank, Last.fm listener counts, and MusicBrainz community ratings so playlists favour well-known tracks over deep cuts (Deezer and MusicBrainz require no API key)
- **Mood & theme tagging** — Essentia audio analysis classifies tracks into mood (happy, calm, dark, energetic, ...) and theme (film, party, summer, ...) tags. Supplemented by Last.fm and MusicBrainz user tags. The AI uses these to match prompts like "chill vibes for a road trip" more accurately.
- **Negative filters** — "NOT", "no", "without" in prompts automatically exclude matching genres, artists, or keywords
- **Artist diversity** — Candidates are capped at 30% of requested song count per artist (min 3) so one artist never dominates; cap is skipped when specific artists are requested
- **Real-time progress** — SSE streaming shows each generation phase as it happens with elapsed time
- **Multiple AI providers** — Claude (Anthropic) or Gemini (Google); switch per-request in the UI when both keys are configured
- **Rich metadata** — Scans BPM, mood, composer, label directly from audio files (richer than the Subsonic API)
- **Multiple media servers** — Save playlists to Navidrome, Plex/Plexamp, or both. Toggle between servers in the UI when both are configured.
- **Export options** — Save to your media server or download as .m3u
- **Settings panel** — Gear icon in header to configure servers, AI keys, models, and more — persists to disk, no restart needed
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

Click the **Settings gear icon** in the header to configure your media servers, AI provider and API keys, and other settings. These are saved to disk and persist across restarts.

The first scan indexes your full library (a few minutes for large collections). Subsequent scans are incremental. Popularity enrichment runs in the background automatically. To enable mood/theme tagging, toggle it on in Settings.

## Unraid Deployment

See [unraid/README.md](unraid/README.md) for detailed instructions. Two options:

### Option A: Unraid User Script (recommended)

1. Install the **User Scripts** plugin from Community Applications
2. Go to **Settings > User Scripts > Add New Script**
3. Paste the contents of `unraid/deploy-navicraft.sh`
4. Edit the configuration block at the top — set your music path and port (server/AI settings are configured in the web UI after first launch)
5. Click **Run Script**

The script always pulls the latest image, prunes old layers, and restarts the container cleanly.

### Option B: Docker Template

1. Copy `unraid/my-navicraft.xml` to `/boot/config/plugins/dockerMan/templates-user/`
2. Go to **Docker > Add Container**, select NaviCraft from the template dropdown
3. Fill in your settings and click **Apply**

## Configuration

Most settings can be configured from the **Settings gear icon** in the web UI. These persist to `/data/navicraft_config.json` and take effect immediately without a restart.

### UI-configurable settings

| Setting | Default | Description |
|---------|---------|-------------|
| Navidrome URL | `http://localhost:4533` | Navidrome server URL |
| Navidrome User | `admin` | Navidrome username |
| Navidrome Password | — | Navidrome password |
| Plex URL | — | Plex server URL (e.g. `http://localhost:32400`) |
| Plex Token | — | Plex authentication token ([how to find](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| AI Provider | `claude` | Default AI provider: `claude` or `gemini` |
| Claude API Key | — | Anthropic API key (requires separate API billing) |
| Claude Model | `claude-3-5-sonnet-20241022` | Claude model identifier |
| Gemini API Key | — | Google AI API key |
| Gemini Model | `gemini-2.5-flash` | Gemini model identifier |
| Last.fm API Key | — | Last.fm API key ([free](https://www.last.fm/api/account/create)) — improves popularity |
| Scan Interval | `6` hours | Background scan interval |
| Mood Scan Enabled | `false` | Enable Essentia-based mood/theme tagging |
| Mood Scan Batch Size | `50` | Number of tracks to process per mood scan run |
| Mood Scan Interval | `24` hours | Hours to wait between mood scan batches (starts after batch completes) |

These can also be set via env vars for initial bootstrap — the UI config overrides them.

### Container environment variables (not in UI)

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_DIR` | `/music` | Music directory inside the container |
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
| **Deezer** | Track rank (0–1M) mapped to 0–100 | Best signal; free, no API key needed |
| **Last.fm** | Listener count + scrobble ratio | Good; free API key |
| **MusicBrainz** | Community ratings (0–5 → 0–100) + vote count | Free, no API key; 1 req/sec rate limit |
| **Track position** | Album position heuristic | +5 for tracks 1–2, +3 for 3–4 |

Deezer and MusicBrainz are always available with no configuration needed. Last.fm is optional but improves accuracy.

## Mood & Theme Tagging

NaviCraft can analyze your audio files locally to classify tracks into **mood tags** (happy, sad, energetic, calm, dark, uplifting, ...) and **theme tags** (film, party, nature, summer, sport, travel, ...). This helps the AI better match prompts like "chill vibes for a road trip" or "energetic workout mix".

**Sources (combined per track):**

| Source | Type | Notes |
|--------|------|-------|
| **Essentia** (MTG-Jamendo) | Local audio analysis | CPU-heavy (~2-5s/track), models auto-download (~80MB) |
| **File tags** | Existing mood field | Reads mood from ID3/Vorbis/FLAC tags already in your files |
| **Last.fm** | `track.getTopTags` API | User-applied tags categorized into mood/theme (needs API key) |
| **MusicBrainz** | Folksonomy tags | Community tags, free, no API key |

**Setup:**

Docker users get Essentia automatically — the Docker image includes `essentia-tensorflow`. If you see the warning `essentia-tensorflow not installed — skipping audio analysis, using API tags only` in your logs, rebuild the image (`docker compose up -d --build`) to pick up the latest Dockerfile which fixes the package version.

For local (non-Docker) development, install the optional dependency:

```bash
pip install essentia-tensorflow==2.1b6.dev1389
```

> **Note:** `essentia-tensorflow` provides pre-built wheels for Linux (x86_64) and macOS (x86_64/arm64) on Python 3.9–3.12. If no wheel is available for your platform, mood scanning falls back to API-only tagging (Last.fm + MusicBrainz + file metadata) — no audio analysis, but still useful.

**How it works:**
- Enable in Settings under "Mood / Theme Tagging"
- Configure batch size (X tracks) and interval (Y hours between batches)
- The scanner processes X tracks, then waits Y hours before the next batch
- On first run, Essentia models (~80MB) are downloaded automatically from `essentia.upf.edu`
- Tags are stored separately as `mood_tags` and `theme_tags` in the database
- The AI uses these tags as filters in Pass 1 and as context in Pass 2

**Troubleshooting:**
- **"essentia-tensorflow not installed"** — Essentia isn't installed or failed to install. For Docker, rebuild the image. For local, run `pip install essentia-tensorflow==2.1b6.dev1389`.
- **"Essentia model download failed"** — The container needs internet access on first run to download models. Check network/firewall settings.
- **Mood scanning is slow** — Audio analysis is CPU-heavy (~2-5s per track). Use a small batch size (e.g. 50) and let it run in the background over time.

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
│   ├── config.py        # Config with JSON persistence + env var fallbacks
│   ├── database.py      # SQLite schema, queries, migrations
│   ├── scanner.py       # mutagen-based file scanner
│   ├── ai_engine.py     # Two-pass AI (Claude / Gemini)
│   ├── navidrome.py     # Subsonic API client
│   ├── plex.py          # Plex HTTP API client
│   ├── popularity.py    # Deezer + Last.fm + MusicBrainz enrichment
│   ├── mood_scanner.py  # Essentia mood/theme tagging + API tag enrichment
│   ├── scheduler.py     # Background scan + enrichment + mood scan jobs
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
| GET | `/api/config` | Get editable config (secrets masked) |
| PUT | `/api/config` | Update config and persist to disk |
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
| POST | `/api/mood/scan` | Manually trigger a mood/theme tag scan batch |
| GET | `/api/mood/status` | Mood scan progress and coverage stats |
| POST | `/api/mood/reset` | Reset all mood/theme tags for re-scanning |
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

- **Tag your music well.** Genre and year are the most impactful tags for playlist quality. BPM and mood help too but are rarer. Enable mood/theme scanning in Settings to auto-tag tracks via audio analysis.
- **Deezer and MusicBrainz work out of the box.** No API keys needed. Add a Last.fm key for even better popularity data and richer mood/theme tags.
- **Use negative filters.** "Jazz but NOT smooth jazz" or "Electronic without EDM" works — the AI extracts exclusions and applies them at the SQL query stage.
- **Claude vs Gemini:** Claude tends to produce more thoughtful, ordered playlists. Gemini is faster and has a generous free tier. Both can be active simultaneously and switched per-request in the UI.
- **Large libraries (50k+):** The two-pass strategy handles this well. If the AI misses songs you'd expect, increase `MAX_CANDIDATES` (uses more tokens per request).
- **Media server ID sync:** NaviCraft matches songs to Navidrome/Plex by file path. If paths differ (e.g. symlinks), it falls back to artist + title matching. When both servers are configured, IDs are synced independently for each.
- **Manual rescan:** Click the ♪ logo mark in the top-left to trigger an incremental rescan at any time.

## License

MIT
