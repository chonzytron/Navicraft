# CLAUDE.md — Project Guide for Claude Code

## What is NaviCraft?

AI-powered playlist generator for Navidrome. It scans a local music library, builds a SQLite index of metadata, then uses a two-pass AI strategy to generate playlists from free-form text prompts.

## Architecture

```
backend/
├── main.py          # FastAPI app, all HTTP routes, startup lifecycle
├── config.py        # Env var config dataclass
├── database.py      # SQLite schema, queries, index operations
├── scanner.py       # mutagen-based file scanner, reads ID3/Vorbis/FLAC/MP4 tags
├── ai_engine.py     # Two-pass AI: intent extraction → SQLite filter → song selection
├── navidrome.py     # Subsonic API client (playlist CRUD + ID sync)
├── scheduler.py     # APScheduler for periodic background scans
└── requirements.txt

frontend/
└── index.html       # Single-file SPA (vanilla HTML/CSS/JS, no build step)
```

## Key Design Decisions

- **Local file scanning** (not Subsonic API) for metadata — gets BPM, mood, composer, label, etc.
- **SQLite index** with incremental updates by file mtime
- **Two-pass AI** to handle 20-50k song libraries without exceeding context limits:
  - Pass 1: prompt + compact library summary → structured filters (genres, years, artists, moods)
  - SQLite query narrows to ~500 candidates
  - Pass 2: prompt + candidate list → final ordered playlist
- **Navidrome only for playlist creation** — songs matched by file path, fallback to artist+title
- **Supports Claude and Gemini** as AI providers via simple config swap

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
- **Add API endpoints**: Add route in `main.py`, models at the top of the file
- **Frontend changes**: Edit `frontend/index.html` directly — it's a single file, no build step

## Testing Notes

- The scanner needs actual music files to test — point `MUSIC_DIR` at a small test collection
- AI calls can be slow (5-15s per pass) — the frontend shows loading state with pass indicators
- Navidrome ID sync requires Navidrome to be running and accessible
- SQLite DB persists at `DB_PATH` (default `/data/navicraft.db`)

## Code Style

- Python: type hints, async/await for IO, logging over print
- Frontend: vanilla JS, no framework, minimal DOM manipulation
- Errors: raise HTTPException with meaningful detail messages
