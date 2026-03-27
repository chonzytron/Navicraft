# CLAUDE.md — Project Guide for Claude Code

## What is NaviCraft?

AI-powered playlist generator for Navidrome. It scans a local music library, builds a SQLite index of metadata + popularity scores, then uses a two-pass AI strategy to generate playlists from free-form text prompts.

## Architecture

```
backend/
├── main.py          # FastAPI app, all HTTP routes, startup lifecycle, SSE streaming, rate limiting
├── config.py        # Env var config dataclass (all settings from .env)
├── database.py      # SQLite schema, queries, migrations, playlist history, popularity columns
├── scanner.py       # mutagen-based file scanner, reads ID3/Vorbis/FLAC/MP4 tags
├── ai_engine.py     # Two-pass AI: intent extraction → SQLite filter → song selection
├── navidrome.py     # Subsonic API client (playlist CRUD + ID sync, with retry logic)
├── popularity.py    # Multi-source enrichment: Last.fm + MusicBrainz + track position heuristic
├── scheduler.py     # APScheduler for periodic scans (6h) and enrichment (10m)
└── requirements.txt

frontend/
└── index.html       # Single-file SPA (vanilla HTML/CSS/JS, no build step)

unraid/
├── deploy-navicraft.sh  # Unraid User Script for automated deployment
├── my-navicraft.xml     # Unraid Docker UI XML template
└── README.md            # Unraid-specific deployment guide

.github/workflows/
└── docker-publish.yml   # CI to publish Docker image to GHCR
```

## Key Design Decisions

- **Local file scanning** (not Subsonic API) for metadata — gets BPM, mood, composer, label, etc.
- **SQLite index** with incremental updates by file mtime, WAL mode for concurrent reads
- **Auto-migration** via `_migrate()` in `database.py` — new columns added automatically on startup
- **Two-pass AI** to handle 20-50k song libraries without exceeding context limits:
  - Pass 1: prompt + compact library summary → structured filters (genres, years, artists, moods, excludes)
  - SQLite query narrows to ~500 candidates, biased by popularity with random jitter
  - Per-artist diversity cap (max 15 tracks per artist) prevents one artist dominating candidates
  - Pass 2: prompt + candidate list → final ordered playlist
- **Negative filters**: Pass 1 returns `exclude_genres`, `exclude_artists`, `exclude_keywords` for "NOT" prompts
- **Popularity scoring**: Multi-source enrichment (Last.fm listeners/playcount, MusicBrainz ratings/release count, track position). Confidence-weighted blending. Skips MusicBrainz when Last.fm has 100k+ listeners.
- **SSE streaming** on `/api/generate` for real-time progress feedback
- **Rate limiting**: 10s cooldown on `/api/generate` to prevent double-clicks
- **Playlist history**: All generated playlists logged locally with "Reuse prompt" and "More like this" in UI
- **Navidrome only for playlist creation** — songs matched by file path, fallback to artist+title
- **Supports Claude and Gemini** as AI providers via simple config swap
- **Retry logic** everywhere: AI calls (3 retries, exponential backoff), Navidrome calls, popularity lookups

## Running Locally

```bash
cd backend
pip install -r requirements.txt
export MUSIC_DIR=/path/to/music
export NAVIDROME_URL=http://localhost:4533
export NAVIDROME_USER=admin
export NAVIDROME_PASSWORD=xxx
export AI_PROVIDER=claude
export CLAUDE_API_KEY=sk-ant-xxx
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
- **Frontend changes**: Edit `frontend/index.html` directly — it's a single file, no build step
- **Schema changes**: Add columns in `SCHEMA` dict in `database.py`, the `_migrate()` function handles adding new columns automatically

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (used by Docker HEALTHCHECK) |
| GET | `/api/navidrome/test` | Test Navidrome connection |
| GET | `/api/library/stats` | Library stats (song/album/artist counts, duration) |
| GET | `/api/library/genres` | List all genres with counts |
| GET | `/api/library/search?q=` | Search tracks by text |
| POST | `/api/scan?full=false` | Trigger library scan |
| GET | `/api/scan/status` | Current scan progress |
| POST | `/api/generate` | Generate playlist via SSE stream (rate limited) |
| POST | `/api/playlists` | Save playlist to Navidrome |
| GET | `/api/playlists` | List Navidrome playlists |
| GET | `/api/playlists/history` | Get local playlist generation history |
| DELETE | `/api/playlists/history/:id` | Delete from local history |
| DELETE | `/api/playlists/:id` | Delete from Navidrome |
| POST | `/api/popularity/re-enrich` | Reset and re-enrich all popularity data |
| GET | `/api/popularity/status` | Enrichment progress (enriched/total/percent) |
| POST | `/api/export/m3u` | Download playlist as .m3u file |

## Testing Notes

- The scanner needs actual music files to test — point `MUSIC_DIR` at a small test collection
- AI calls can be slow (5-15s per pass) — the frontend shows SSE streaming progress with elapsed timer
- Navidrome ID sync requires Navidrome to be running and accessible
- SQLite DB persists at `DB_PATH` (default `/data/navicraft.db`)
- Popularity enrichment runs in background every 10 minutes (batch of 500)

## Code Style

- Python: type hints, async/await for IO, logging over print
- Frontend: vanilla JS, no framework, minimal DOM manipulation
- Errors: raise HTTPException with meaningful detail messages
- Retries: exponential backoff for all external API calls
