# NaviCraft

AI-powered playlist generator for [Navidrome](https://www.navidrome.org/) and [Plex/Plexamp](https://www.plex.tv/). Describe the vibe you want, get a playlist built from your own music library.

## How It Works

```
SCAN       Walk /music → read tags with mutagen → SQLite index
ENRICH     Deezer + Last.fm + MusicBrainz → popularity scores 0–100 (background)
MOOD TAG   Essentia audio analysis → mood/theme tags with confidence (optional, background)
GENERATE   Two-pass AI: extract filters → query candidates → AI picks & orders
CREATE     Save to Navidrome / Plex, or export .m3u
```

**Why two passes?** A 30k-song library won't fit in one AI prompt. Pass 1 turns your prompt plus a compact library summary into structured filters (genres, era, artists, moods, tempo, keywords, exclusions). SQLite narrows the library to up to 500 candidates. Pass 2 sees full metadata for those candidates plus the filter context, then picks and orders the final playlist — cross-checking genre fidelity and mixing well-known tracks with lesser-known ones.

**How candidates stay relevant:**

- **Token-boundary matching** — "Queen" won't pull in "Queens of the Stone Age" (exact matches rank first), and excluding "rap" won't drop "trap".
- **Multi-genre aware** — every genre on a track is indexed, so "Rock; Alternative" matches both rock and alternative prompts.
- **Popularity variety** — candidates are drawn proportionally from popular / mid / niche tiers, and a per-artist cap (30% of requested songs, min 3) stops one artist dominating.
- **Progressive relaxation** — too few matches? Drop moods/tempo/keywords first, then keep the era while broadening genre, then genre+artists only, then unfiltered. "Best of"-style requests never drop their defining artist, genre, or decade.
- **Popularity mode** — "best of X" / "top hits" prompts rank candidates strictly by popularity.
- **Duration-aware sizing** — duration-targeted playlists size the candidate pool from the target length, then trim or pad to within ±5 minutes.

## Features

- **Natural language prompts** — "Upbeat indie rock for a summer road trip", "Jazz but NOT smooth jazz"
- **Negative filters** — "NOT" / "no" / "without" exclude genres, artists, or keywords at the SQL stage
- **Mood & theme tagging** — Essentia classifies tracks into a standardized 57-tag vocabulary (31 moods + 26 themes) with confidence scores
- **Two AI providers** — Claude (Anthropic) or Gemini (Google), switchable per request when both are configured
- **Two media servers** — save to Navidrome, Plex/Plexamp, or both; or download as .m3u
- **Playlist watcher** — name an empty Navidrome playlist `your prompt [navicraft]` and it's generated in place, no UI needed
- **Live progress** — SSE streaming shows each generation phase as it happens
- **Settings panel** — servers, keys, models, and schedules configurable in the UI; persists to disk and takes effect immediately, no restart

## Quick Start (Docker)

```bash
git clone https://github.com/chonzytron/navicraft.git
cd navicraft
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:8085` and click the **Settings gear icon** to configure media servers, AI provider, and API keys.

The first scan indexes your full library (a few minutes for large collections); later scans are incremental. Popularity enrichment runs automatically in the background. Mood/theme tagging is enabled in Settings.

**Unraid:** see [unraid/README.md](unraid/README.md) — a User Script (recommended) and a Docker XML template are provided.

## Configuration

Most settings live in the **Settings gear icon** in the web UI. They persist to `/data/navicraft_config.json` and take effect immediately. Env vars set initial defaults; UI config overrides them.

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
| Claude Model | `claude-sonnet-4-6` | Claude model identifier |
| Gemini API Key | — | Google AI API key |
| Gemini Model | `gemini-2.5-flash` | Gemini model identifier |
| Last.fm API Key | — | Last.fm API key ([free](https://www.last.fm/api/account/create)) — improves popularity |
| Scan Interval | `6` hours | Background scan interval |
| Timezone | `UTC` | IANA timezone for schedule windows (e.g. `America/New_York`) |
| Mood Scan Enabled | `false` | Enable Essentia-based mood/theme tagging |
| Mood Scan From/To Hour | `0` / `6` | Schedule window (hours 0–23) |
| Playlist Watcher Enabled | `false` | Enable Navidrome `[navicraft]` playlist detection |
| Playlist Watcher Interval | `30` seconds | Poll interval for `[navicraft]` playlists (10–300s) |

### Container environment variables (not in UI)

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_DIR` | `/music` | Music directory inside the container |
| `SCAN_EXTENSIONS` | `.mp3,.flac,.ogg,.opus,.m4a,.wma,.aac,.wav,.aiff,.ape,.wv,.mpc` | File types to index |
| `MAX_CANDIDATES` | `500` | Max songs passed to AI Pass 2 |
| `MOOD_SCAN_BATCH_SIZE` | `50` | Tracks processed per mood scan run |
| `DB_PATH` | `/data/navicraft.db` | SQLite database path |

### Docker Compose host variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_PATH` | `/mnt/user/media/music` | Host path mounted read-only at `/music` |
| `APPDATA_PATH` | `./data` | Host path for persistent data (SQLite DB) |
| `NAVICRAFT_PORT` | `8085` | Host port |

> **Claude API vs Claude.ai:** A Claude Pro/Team subscription does **not** include API access — the API requires separate credits at [console.anthropic.com](https://console.anthropic.com). Gemini has a generous free tier and works well as an alternative.

## Popularity Enrichment

Each track is scored 0–100 from up to four signals, blended by confidence:

| Source | Signal | Notes |
|--------|--------|-------|
| **Deezer** | Track rank (0–1M) mapped to 0–100 | Best signal; free, no API key |
| **Last.fm** | Listener count + scrobble ratio | Good; free API key |
| **MusicBrainz** | Community ratings (0–5 → 0–100) + vote count | Free, no API key; 1 req/sec limit |
| **Track position** | Album position heuristic | +5 for tracks 1–2, +3 for 3–4 |

Deezer and MusicBrainz need no configuration. Lookups only accept results whose artist actually matches — a track that isn't found keeps no score from a lookalike and retries daily. Enrichment runs every 2 minutes in the background until the library is covered.

## Mood & Theme Tagging

Essentia (MTG-Jamendo model) analyzes audio locally and classifies each track into **mood tags** (happy, sad, energetic, calm, ...) and **theme tags** (film, party, summer, sport, ...) with confidence scores, e.g. `happy:0.85, energetic:0.72`. The standardized 57-tag vocabulary is given to the AI in Pass 1 so prompts map to filters that actually exist in your library; Pass 2 sees compact tag names per candidate.

**Setup:** the Docker image includes `essentia-tensorflow`. Enable tagging in Settings, set a schedule window (e.g. overnight), and batches run back-to-back inside it — or use the play/pause button on the mood progress bar to run continuously. Models (~80MB) download automatically from `essentia.upf.edu` on first run.

For local (non-Docker) development, Essentia needs the `--pre` flag (pre-release versioning); wheels exist for Linux x86_64 and macOS on Python 3.9–3.12:

```bash
pip install --pre -r requirements.txt
```

**Troubleshooting:**
- `essentia-tensorflow not available` — rebuild the Docker image, or reinstall with `--pre` locally.
- Model download failed — the container needs internet access on first run.
- Scanning is slow — analysis is CPU-heavy (~2–5s per track); let it run in the background over time.

## Navidrome Playlist Watcher

Generate playlists without leaving Navidrome: create an **empty** playlist whose name ends in `[navicraft]`, and NaviCraft detects it, generates songs, populates it, and renames it to an AI-chosen name. Works from any Subsonic client (Navidrome web UI, Feishin, Symfonium, ...).

Enable **Playlist Watcher** in Settings (Navidrome connection must be green). Name format:

```
<your prompt> [navicraft]
<your prompt> [navicraft, songs: 40]
<your prompt> [navicraft, duration: 90]
<your prompt> [navicraft, songs: 30, duration: 60]
```

| Parameter | Range | Default | Description |
|-----------|-------|---------|-------------|
| `songs` | 5–100 | 25 | Number of songs to generate |
| `duration` | 5–600 | — | Target minutes (overrides song count for sizing) |

Examples: `best of Radiohead [navicraft]` → 25 tracks, popularity-ranked. `90s hip hop NOT gangsta rap [navicraft, songs: 30]` → 30 tracks with the exclusion applied.

Only empty playlists are processed (prevents re-triggering), and processed IDs are remembered. If a playlist isn't detected, check that the watcher is enabled and the name contains `[navicraft]`; if no songs are added, check `docker logs navicraft | grep watcher` and `GET /api/watcher/status`.

## Metadata Extracted

Read directly from files with `mutagen` — richer than the Subsonic API exposes:

- Title, Artist, Album Artist, Album
- Genre (all genres on a track, not just the first), Year (original release date preferred over reissue dates), Track / Disc number
- Duration, BPM, Sample rate, Bitrate
- Composer, Mood, Comment (encoder/normalization junk filtered out), Label
- File format, path, size

## Architecture

```
navicraft/
├── backend/
│   ├── main.py                  # FastAPI routes, SSE streaming, rate limiting
│   ├── config.py                # Config with JSON persistence + env var fallbacks
│   ├── database.py              # SQLite schema, queries, migrations
│   ├── scanner.py               # mutagen-based file scanner
│   ├── ai_engine.py             # Two-pass AI (Claude / Gemini)
│   ├── generation_pipeline.py   # Shared generation flow: filters, relaxation, duration
│   ├── navidrome.py             # Subsonic API client (playlist CRUD + ID sync)
│   ├── plex.py                  # Plex HTTP API client
│   ├── popularity.py            # Deezer + Last.fm + MusicBrainz enrichment
│   ├── mood_scanner.py          # Essentia mood/theme tagging with confidence scores
│   ├── playlist_watcher.py      # Navidrome [navicraft] playlist detection
│   └── scheduler.py             # Background scan + enrichment + mood scan + watcher jobs
├── frontend/                    # SPA, no build step (index.html + app.js + styles.css)
├── unraid/                      # Deploy script, Docker template, README
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
| POST | `/api/popularity/enrich` | Trigger an enrichment batch |
| POST | `/api/popularity/re-enrich` | Reset and re-enrich all popularity data |
| GET | `/api/popularity/status` | Enrichment progress per source |
| POST | `/api/mood/scan` | Trigger a mood/theme scan batch |
| GET | `/api/mood/status` | Mood scan progress and coverage |
| POST | `/api/mood/reset` | Reset all mood/theme tags |
| POST | `/api/mood/continuous` | Start/stop continuous mood scanning |
| GET | `/api/watcher/status` | Watcher status and generation history |

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

The response is an SSE stream: `progress` events per phase, then a `result` event with the playlist (or an `error` event with the message).

## Tips

- **Tag your music well.** Genre and year matter most; BPM and mood help too. Multi-genre tags like `Rock; Alternative` are fully indexed (older tracks pick up extra genres on the next full rescan). Enable mood scanning to auto-tag via audio analysis.
- **Popularity works out of the box** via Deezer and MusicBrainz; a free Last.fm key improves it. `POST /api/popularity/re-enrich` rebuilds all scores from scratch.
- **Negative filters and keywords work.** "Jazz but NOT smooth jazz" excludes at the SQL stage; "songs about love" matches titles, albums, and comments.
- **Claude vs Gemini:** Claude tends to produce more thoughtful ordering; Gemini is fast with a generous free tier. Switch per request when both are configured.
- **Large libraries (50k+):** if the AI misses expected songs, raise `MAX_CANDIDATES` (more tokens per request).
- **ID sync:** songs match to Navidrome/Plex by file path, with artist+title fallback (useful with symlinks). Each server syncs independently.
- **Manual rescan:** click the ♪ logo in the header.

## License

MIT
