# CLAUDE.md — Project Guide for Claude Code

## What is NaviCraft?

AI-powered playlist generator for Navidrome. Scans a local music library, builds a SQLite index of metadata + popularity scores, then uses a two-pass AI strategy to generate playlists from free-form text prompts.

## Architecture

```
backend/
├── main.py          # FastAPI app, all HTTP routes, startup lifecycle, SSE streaming, rate limiting
├── config.py        # Env var config dataclass (all settings from .env)
├── database.py      # SQLite schema, queries, migrations, bulk update helpers
├── scanner.py       # mutagen-based file scanner, reads ID3/Vorbis/FLAC/MP4 tags
├── ai_engine.py     # Two-pass AI: intent extraction → SQLite filter → song selection
├── navidrome.py     # Subsonic API client (playlist CRUD + ID sync, with retry logic)
├── popularity.py    # Multi-source enrichment: Spotify + Last.fm + MusicBrainz + track position
├── scheduler.py     # APScheduler for periodic scans (configurable, default 6h) and enrichment (2m)
└── requirements.txt

frontend/
└── index.html       # Single-file SPA (vanilla HTML/CSS/JS, no build step)

unraid/
├── deploy-navicraft.sh  # Unraid User Script for automated deployment
├── my-navicraft.xml     # Unraid Docker UI XML template
└── README.md            # Unraid-specific deployment guide

.github/workflows/
└── docker-publish.yml   # CI to publish Docker image to GHCR on push to main / version tags
```

## Key Design Decisions

- **Local file scanning** (not Subsonic API) for metadata — gets BPM, mood, composer, label, etc.
- **SQLite index** with incremental updates by file mtime, WAL mode for concurrent reads
- **Auto-migration** via `_migrate()` in `database.py` — new columns added automatically on startup
- **Two-pass AI** to handle 20–50k song libraries without exceeding context limits:
  - Pass 1: prompt + compact library summary → structured filters (genres, years, artists, moods, excludes)
  - SQLite query narrows to ~500 candidates, biased by popularity with random jitter
  - Per-artist diversity cap (max 15 tracks per artist) prevents one artist dominating candidates
  - Pass 2: prompt + candidate list → final ordered playlist
- **Negative filters**: Pass 1 returns `exclude_genres`, `exclude_artists`, `exclude_keywords` for "NOT" prompts
- **Popularity scoring**: Multi-source enrichment (Spotify streaming popularity, Last.fm listeners/playcount, MusicBrainz ratings/release count, track position heuristic). Confidence-weighted blending. MusicBrainz skipped when Spotify/Last.fm already have good signal.
- **Spotify rate limiting**: `_spotify_blocked_until` global timestamp; honors `Retry-After` header from 429 responses (can be hours-long). On cooldown, requests fast-fail without sleeping. Cooldown is persisted to the `settings` DB table so container restarts don't trigger a wasted new attempt during an active server-side block.
- **Spotify batch lookups**: Once a track's `spotify_id` is stored from the initial search, subsequent top-up passes call `GET /v1/tracks?ids=...` with up to 50 IDs per request — 50× more efficient than individual searches.
- **Spotify top-up pass**: After each enrichment batch, tracks with `popularity IS NOT NULL` but `spotify_popularity IS NULL` are retroactively enriched. Tracks with a known `spotify_id` use the batch endpoint; unknown IDs fall back to search.
- **Last.fm top-up pass**: Mirrors Spotify top-up — after the main batch, tracks with `lastfm_listeners IS NULL` are retroactively enriched using Last.fm search.
- **24h not-found backoff**: Lookup functions distinguish confirmed-absent tracks (200 OK, empty results → `{"not_found": True}`) from transient errors (`None`). Confirmed-absent tracks have `spotify_checked_at` / `lastfm_checked_at` set and are skipped for 24 hours before retry. Transient errors retry normally on the next enrichment cycle.
- **Per-track checked timestamps**: `spotify_checked_at` and `lastfm_checked_at` columns (Unix timestamp) track the last definitive API check per source. Missing-source queries filter: `checked_at IS NULL OR (unixepoch() - checked_at) > 86400`.
- **Settings table**: `settings (key TEXT PRIMARY KEY, value TEXT)` in SQLite for persistent key-value config. Currently used for Spotify cooldown persistence. Accessed via `db.get_setting()` / `db.set_setting()`.
- **Enrichment lock**: `asyncio.Lock` in `popularity.py` prevents concurrent enrichment runs (startup scan, post-scan trigger, and scheduler all call `enrich_popularity`).
- **Scheduler checks all sources**: `_scheduled_enrichment()` checks `count_tracks_without_popularity`, `count_tracks_missing_spotify`, and `count_tracks_missing_lastfm` before early-exiting — ensuring top-up passes run even when all tracks have a base score.
- **SSE streaming** on `/api/generate` for real-time progress feedback. Includes `X-Accel-Buffering: no` and `Cache-Control: no-cache` headers to prevent Nginx proxy buffering.
- **Rate limiting**: 10s cooldown on `/api/generate` to prevent double-clicks
- **Navidrome only for playlist creation** — songs matched by file path, fallback to artist+title
- **Supports Claude and Gemini** as AI providers, switchable per-request via optional `provider` field
- **Retry logic** everywhere: AI calls (3 retries, exponential backoff), Navidrome calls, popularity lookups
- **Song ID normalisation**: AI responses may return song IDs as strings; backend casts to `int` before candidate map lookup to prevent mismatches.
- **AI errors surfaced to UI**: API error messages are extracted from JSON responses and raised as `ValueError` so they propagate through the SSE error event to the frontend instead of showing a generic "check logs" message.

## Running Locally

```bash
cd backend
pip install -r requirements.txt
export MUSIC_DIR=/path/to/music
export NAVIDROME_URL=http://localhost:4533
export NAVIDROME_USER=admin
export NAVIDROME_PASSWORD=xxx
export AI_PROVIDER=claude          # or gemini
export CLAUDE_API_KEY=sk-ant-xxx   # Anthropic API key (separate from Claude.ai subscription)
uvicorn main:app --reload --port 8085
```

## Running with Docker

```bash
cp .env.example .env
# edit .env with your settings
docker compose up -d --build
```

## Common Development Tasks

- **Add a new AI provider**: Add a `_call_newprovider()` function in `ai_engine.py`, add config vars in `config.py`, add routing in `_call_ai()`
- **Add metadata fields**: Update `SCHEMA` in `database.py`, update `_extract_metadata()` in `scanner.py`, update `filter_tracks()` if the field should be filterable
- **Add API endpoints**: Add route in `main.py`, Pydantic models at the top of the file
- **Add new popularity source**: Add lookup function in `popularity.py`, integrate into `_blend_scores()`, add DB columns in `database.py` with migration support
- **Frontend changes**: Edit `frontend/index.html` directly — single file, no build step
- **Schema changes**: Add columns to `SCHEMA` dict in `database.py`; `_migrate()` handles adding new columns automatically on startup

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (used by Docker HEALTHCHECK) |
| GET | `/api/ai/providers` | List configured providers with model names |
| GET | `/api/navidrome/test` | Test Navidrome connection |
| GET | `/api/library/stats` | Library stats (song/album/artist counts, duration, genres) |
| GET | `/api/library/genres` | List all genres with counts |
| GET | `/api/library/search?q=` | Search tracks by text (max 50 results) |
| POST | `/api/scan?full=false` | Trigger library scan (incremental or full) |
| GET | `/api/scan/status` | Current scan progress |
| POST | `/api/generate` | Generate playlist via SSE stream (rate limited 10s) |
| POST | `/api/playlists` | Save playlist to Navidrome |
| GET | `/api/playlists` | List Navidrome playlists |
| DELETE | `/api/playlists/:id` | Delete from Navidrome |
| POST | `/api/popularity/enrich` | Manually trigger an enrichment batch |
| POST | `/api/popularity/re-enrich` | Reset and re-enrich all popularity data |
| GET | `/api/popularity/status` | Per-source enrichment progress: total, enriched/remaining/percent for overall, Spotify, and Last.fm; `running` flag |
| POST | `/api/export/m3u` | Download playlist as .m3u file |

## Key Defaults

| Setting | Value | Location |
|---------|-------|----------|
| Default AI provider | `claude` | config.py |
| Claude model | `claude-3-5-sonnet-20241022` | config.py |
| Gemini model | `gemini-2.5-flash` | config.py |
| Max candidates (Pass 2) | `500` | config.py |
| Claude max_tokens | `8192` | ai_engine.py |
| Gemini max_tokens | `32768` | ai_engine.py |
| AI request timeout | `180s` | ai_engine.py |
| Generation rate limit | `10s` | main.py |
| Per-artist diversity cap | `15 tracks` | database.py |
| Enrichment batch size | `500 tracks` | scheduler / main.py |
| DB write batch size | `50 tracks` | popularity.py |
| Enrichment job interval | `2 minutes` | scheduler.py |
| Scan job interval | `6 hours` (configurable) | scheduler.py / config.py |
| Spotify delay | `0.5s` (2 req/s) | popularity.py |
| Last.fm delay | `0.25s` (4 req/s) | popularity.py |
| MusicBrainz delay | `1.1s` | popularity.py |
| Spotify cooldown on 429 | `Retry-After` value (fallback 600s), persisted to DB | popularity.py |
| Not-found retry window | `86400s (24h)` per track per source | database.py |
| Default songs in UI | `30` | frontend/index.html |
| Default duration in UI | `90 min` | frontend/index.html |

## Frontend Features

- **Single-file SPA** — `frontend/index.html`, vanilla JS, no build step
- **Navidrome status indicator** — green/red dot in header next to logo; click to retest connection
- **Rescan trigger** — click the ♪ logo mark to trigger an incremental library scan
- **AI provider selector** — pill toggle (Claude / Gemini) shown only when both keys are configured
- **Mode toggle** — Songs (count) or Duration (minutes), one input visible at a time; number inputs have no spinners, clamp to 1–999
- **Preview toggle** — when ON, shows results before saving; when OFF, auto-saves to Navidrome on generation
- **SSE progress display** — real-time phase labels and elapsed timer during generation
- **Dual enrichment progress bars** — Last.fm (purple) and Spotify (green) bars shown independently while background enrichment is running for either source; each shows percent complete
- **Export** — Save to Navidrome or download as .m3u

## Testing Notes

- The scanner needs actual music files — point `MUSIC_DIR` at a small test collection
- AI calls are slow (5–30s per pass) — the frontend shows SSE streaming progress with elapsed timer
- Navidrome ID sync requires Navidrome to be running and accessible at the configured URL
- SQLite DB persists at `DB_PATH` (default `/data/navicraft.db`)
- Popularity enrichment runs in the background every 2 minutes (500 track batches)
- In Docker on Unraid, `NAVIDROME_URL` must use the host IP, not `localhost`

## Code Style

- Python: type hints, async/await for IO, logging over print
- Frontend: vanilla JS, no framework, minimal DOM manipulation
- Errors: raise `ValueError` with meaningful messages (propagates to SSE error events); raise `HTTPException` for HTTP-layer errors
- Retries: exponential backoff for all external API calls
- No speculative abstractions — only add complexity when the task actually requires it
