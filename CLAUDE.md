# CLAUDE.md — Project Guide for Claude Code

## What is NaviCraft?

AI-powered playlist generator for Navidrome and Plex/Plexamp. Scans a local music library, builds a SQLite index of metadata + popularity scores, then uses a two-pass AI strategy to generate playlists from free-form text prompts. Supports both Navidrome (Subsonic API) and Plex (Plex HTTP API) as playlist targets — one or both can be configured simultaneously.

## Architecture

```
backend/
├── main.py          # FastAPI app, all HTTP routes, startup lifecycle, SSE streaming, rate limiting
├── config.py        # Config dataclass with JSON file persistence + env var fallbacks
├── database.py      # SQLite schema, queries, migrations, bulk update helpers
├── scanner.py       # mutagen-based file scanner, reads ID3/Vorbis/FLAC/MP4 tags
├── ai_engine.py     # Two-pass AI: intent extraction → SQLite filter → song selection
├── navidrome.py     # Subsonic API client (playlist CRUD + ID sync, with retry logic)
├── plex.py          # Plex HTTP API client (playlist CRUD + ID sync, with retry logic)
├── popularity.py    # Multi-source enrichment: Deezer + Last.fm + MusicBrainz + track position
├── mood_scanner.py  # Essentia MTG-Jamendo mood/theme tagging + Last.fm/MusicBrainz tag enrichment
├── scheduler.py     # APScheduler for periodic scans (configurable, default 6h), enrichment (2m), mood scanning
└── requirements.txt

frontend/
├── index.html       # SPA markup (vanilla HTML, no build step)
└── assets/
    ├── app.js       # Frontend logic (vanilla JS)
    └── styles.css   # Styles

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
  - Per-artist diversity cap (30% of requested songs, min 3) prevents one artist dominating candidates; skipped when specific artists are requested
  - Pass 2: prompt + candidate list → final ordered playlist
- **Negative filters**: Pass 1 returns `exclude_genres`, `exclude_artists`, `exclude_keywords` for "NOT" prompts
- **Popularity scoring**: Multi-source enrichment (Deezer rank, Last.fm listeners/playcount, MusicBrainz community ratings, track position heuristic). Confidence-weighted blending. Deezer and MusicBrainz are free with no auth needed.
- **Deezer rate limiting**: On 429 or quota error, waits 5s and retries. After recovery, slows to 0.5s delay for the rest of the batch.
- **Deezer top-up pass**: After each enrichment batch, tracks with `popularity IS NOT NULL` but `deezer_rank IS NULL` are retroactively enriched using stored Last.fm/MusicBrainz values for reblending.
- **MusicBrainz integration**: Community ratings (0-5 scale, converted to 0-100) with vote count as confidence weight. Free API, no auth needed, strict 1 req/sec rate limit. Has its own top-up pass like Deezer and Last.fm.
- **Enrichment lock**: `asyncio.Lock` in `popularity.py` prevents concurrent enrichment runs (startup scan, post-scan trigger, and scheduler all call `enrich_popularity`).
- **Mood/theme tagging**: Essentia MTG-Jamendo model classifies tracks into mood tags (happy, sad, energetic, calm, etc.) and theme tags (film, party, nature, summer, etc.). Supplements audio analysis with file metadata mood field, Last.fm `track.getTopTags`, and MusicBrainz folksonomy tags. Tags are categorized into mood vs theme via a ~100-entry alias map. Stored in separate `mood_tags` and `theme_tags` columns. Configurable: enable/disable, batch size (X tracks), interval (Y hours after batch completes). Models auto-download from `essentia.upf.edu` on first use (~80MB). Graceful fallback to API-only tagging if `essentia-tensorflow` not installed.
- **Mood scan scheduling**: Uses one-shot `DateTrigger` pattern — after each batch of X tracks completes, the next run is scheduled Y hours later. This ensures the interval is the gap between batches, not between starts.
- **SSE streaming** on `/api/generate` for real-time progress feedback. Includes `X-Accel-Buffering: no` and `Cache-Control: no-cache` headers to prevent Nginx proxy buffering.
- **Rate limiting**: 10s cooldown on `/api/generate` to prevent double-clicks
- **Multi-server support**: Both Navidrome and Plex/Plexamp supported as playlist targets. Songs matched by file path, fallback to artist+title. Server selector shown in UI when both are configured.
- **Plex integration**: Uses Plex HTTP API with `X-Plex-Token` auth. Tracks matched via `media[].part[].file` path. Playlist creation uses `server://` URI scheme with machine identifier.
- **Supports Claude and Gemini** as AI providers, switchable per-request via optional `provider` field
- **Retry logic** everywhere: AI calls (3 retries, exponential backoff), Navidrome/Plex calls, popularity lookups
- **Song ID normalisation**: AI responses may return song IDs as strings; backend casts to `int` before candidate map lookup to prevent mismatches.
- **AI errors surfaced to UI**: API error messages are extracted from JSON responses and raised as `ValueError` so they propagate through the SSE error event to the frontend instead of showing a generic "check logs" message.
- **UI-configurable settings**: Navidrome, Plex, AI provider/keys/models, Last.fm API key, scan interval, and mood scan settings are configurable from the Settings gear icon in the web UI. Saved to `/data/navicraft_config.json`. Env vars act as initial defaults; JSON config takes precedence. Secrets are masked in API responses.

## Running Locally

```bash
cd backend
pip install -r requirements.txt
# Optional: enable Essentia audio analysis for mood/theme tagging
pip install essentia-tensorflow==2.1b6.dev1389
export MUSIC_DIR=/path/to/music
uvicorn main:app --reload --port 8085
```

On first launch, click the **Settings gear icon** in the header to configure Navidrome/Plex connections, AI provider and API keys, etc. These persist to a JSON config file.

Alternatively, set env vars as initial defaults (the UI config overrides them):

```bash
export NAVIDROME_URL=http://localhost:4533
export NAVIDROME_USER=admin
export NAVIDROME_PASSWORD=xxx
export AI_PROVIDER=claude
export CLAUDE_API_KEY=sk-ant-xxx
```

## Running with Docker

```bash
cp .env.example .env
# edit .env with your settings
docker compose up -d --build
```

## Common Development Tasks

- **Add a new media server**: Create a new module (like `plex.py`) implementing `test_connection()`, `sync_*_ids()`, `create_playlist()`, `get_playlists()`, `delete_playlist()`. Add config vars in `config.py` (add to `EDITABLE_FIELDS`/`SECRET_FIELDS` and `reload_from_file()`), add DB column + migration in `database.py`, add routes in `main.py`, update `scheduler.py`, add frontend server button, and add fields to the config modal in `index.html` + `cfgFieldMap` in `app.js`.
- **Add a new AI provider**: Add a `_call_newprovider()` function in `ai_engine.py`, add config vars in `config.py`, add routing in `_call_ai()`
- **Add metadata fields**: Update `SCHEMA` in `database.py`, update `_extract_metadata()` in `scanner.py`, update `filter_tracks()` if the field should be filterable
- **Add API endpoints**: Add route in `main.py`, Pydantic models at the top of the file
- **Add new popularity source**: Add lookup function in `popularity.py`, integrate into `_blend_scores()`, add DB columns in `database.py` with migration support
- **Add new mood/theme tag source**: Add lookup function in `mood_scanner.py`, add tag aliases to `LASTFM_MOOD_ALIASES`/`LASTFM_THEME_ALIASES` or `MOOD_CATEGORY`/`THEME_CATEGORY` sets
- **Frontend changes**: Edit `frontend/index.html` (markup), `frontend/assets/app.js` (logic), or `frontend/assets/styles.css` — no build step
- **Schema changes**: Add columns to `SCHEMA` dict in `database.py`; `_migrate()` handles adding new columns automatically on startup

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (used by Docker HEALTHCHECK) |
| GET | `/api/config` | Get current editable config (secrets masked) |
| PUT | `/api/config` | Update config and persist to JSON file |
| GET | `/api/ai/providers` | List configured providers with model names |
| GET | `/api/navidrome/test` | Test Navidrome connection |
| GET | `/api/plex/test` | Test Plex connection |
| GET | `/api/servers` | List configured media servers |
| GET | `/api/library/stats` | Library stats (song/album/artist counts, duration, genres) |
| GET | `/api/library/genres` | List all genres with counts |
| GET | `/api/library/search?q=` | Search tracks by text (max 50 results) |
| POST | `/api/scan?full=false` | Trigger library scan (incremental or full) |
| GET | `/api/scan/status` | Current scan progress |
| POST | `/api/generate` | Generate playlist via SSE stream (rate limited 10s) |
| POST | `/api/playlists` | Save playlist to media server (accepts `server` param) |
| GET | `/api/playlists` | List playlists from media server (accepts `server` query) |
| DELETE | `/api/playlists/:id` | Delete playlist (accepts `server` query) |
| POST | `/api/popularity/enrich` | Manually trigger an enrichment batch |
| POST | `/api/popularity/re-enrich` | Reset and re-enrich all popularity data |
| GET | `/api/popularity/status` | Enrichment progress (enriched/total/percent/running) |
| POST | `/api/mood/scan` | Manually trigger a mood/theme tag scan batch |
| GET | `/api/mood/status` | Mood scan progress (scanned/total/percent/running) |
| POST | `/api/mood/reset` | Reset all mood/theme tags for re-scanning |
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
| Per-artist diversity cap | `30% of max_songs` (min 3, skipped for artist-specific prompts) | database.py |
| Enrichment batch size | `500 tracks` | scheduler / main.py |
| DB write batch size | `50 tracks` | popularity.py |
| Enrichment job interval | `2 minutes` | scheduler.py |
| Scan job interval | `6 hours` (configurable) | scheduler.py / config.py |
| Deezer delay | `0.1s` (10 req/s), `0.5s` after rate limit recovery | popularity.py |
| Last.fm delay | `0.25s` (4 req/s) | popularity.py |
| MusicBrainz delay | `1.1s` (strict 1 req/s) | popularity.py |
| Mood scan enabled | `false` | config.py |
| Mood scan batch size | `50 tracks` | config.py |
| Mood scan interval | `24 hours` (after batch completes) | config.py / scheduler.py |
| Essentia confidence threshold | `0.1` | mood_scanner.py |
| Last.fm tag min count | `10` | mood_scanner.py |
| Default songs in UI | `30` | frontend/index.html |
| Default duration in UI | `90 min` | frontend/index.html |

## Frontend Features

- **SPA** — `frontend/index.html` (markup) + `assets/app.js` (logic) + `assets/styles.css`, vanilla JS, no build step
- **Settings modal** — gear icon in header opens config panel for Navidrome, Plex, AI provider/keys/models, Last.fm, scan interval, and mood/theme tagging settings; persists to `/data/navicraft_config.json`
- **Media server status indicators** — green/red dots in header for each configured server (Navidrome and/or Plex); click to retest
- **Server selector** — pill toggle (Navidrome / Plex) shown when both servers are configured; controls where playlists are saved
- **Rescan trigger** — click the ♪ logo mark to trigger an incremental library scan
- **AI provider selector** — pill toggle (Claude / Gemini) shown only when both keys are configured
- **Mode toggle** — Songs (count) or Duration (minutes), one input visible at a time; number inputs have no spinners, clamp to 1–999
- **Preview toggle** — when ON, shows results before saving; when OFF, auto-saves to selected media server on generation
- **SSE progress display** — real-time phase labels and elapsed timer during generation
- **Enrichment progress bars** — per-source (Deezer, Last.fm, MusicBrainz) progress shown while background enrichment is running
- **Mood scan progress bar** — shows mood/theme tagging progress when enabled
- **Export** — Save to Navidrome/Plex or download as .m3u

## Testing Notes

- The scanner needs actual music files — point `MUSIC_DIR` at a small test collection
- AI calls are slow (5–30s per pass) — the frontend shows SSE streaming progress with elapsed timer
- Media server ID sync requires Navidrome/Plex to be running and accessible at the configured URL
- SQLite DB persists at `DB_PATH` (default `/data/navicraft.db`)
- Popularity enrichment runs in the background every 2 minutes (500 track batches)
- Mood scanning requires `essentia-tensorflow` (`pip install essentia-tensorflow==2.1b6.dev1389`). Models (~80MB) auto-download on first run. CPU-heavy (~2-5s per track). Falls back to API-only tagging if not installed.
- In Docker on Unraid, `NAVIDROME_URL` must use the host IP, not `localhost`

## Code Style

- Python: type hints, async/await for IO, logging over print
- Frontend: vanilla JS, no framework, minimal DOM manipulation
- Errors: raise `ValueError` with meaningful messages (propagates to SSE error events); raise `HTTPException` for HTTP-layer errors
- Retries: exponential backoff for all external API calls
- No speculative abstractions — only add complexity when the task actually requires it
