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
    + theme tags (film, party, nature, summer, ...) with confidence scores.
    Standardized vocabulary of 57 tags ensures consistent, reliable filtering.
    Configurable: batch size, schedule window (e.g. overnight), or continuous mode. Enable in Settings.

3. GENERATE (two-pass AI)
   Pass 1: prompt + library summary → structured filters (genres, era, mood, tempo, keywords, exclusions)
   SQLite query narrows to up to 500 candidates, biased by popularity with random jitter
   Progressive filter relaxation if not enough matches: drop moods/bpm/keywords → drop year range → genre+artists only → unfiltered
   Per-artist diversity cap (30% of requested songs, min 3) prevents one artist dominating candidates
   Pass 2: prompt + candidate list + search filter context → AI picks & orders the final playlist, cross-checking genre fidelity and mixing well-known with lesser-known artists

4. CREATE PLAYLIST
   Match songs to Navidrome IDs → Subsonic createPlaylist
   Or match to Plex ratingKeys → Plex HTTP API createPlaylist
   Or export as .m3u file for any music player
```

**Why two passes?** A 30k song library won't fit in a single AI prompt. Pass 1 uses a compact library summary to identify *what* to look for. SQLite narrows to up to 500 candidates. Pass 2 gets full metadata for those candidates plus the original search filters, and selects the final playlist with genre fidelity, good flow, and a mix of popular and lesser-known artists.

## Features

- **Natural language prompts** — "Upbeat indie rock for a summer road trip" or "Jazz but NOT smooth jazz"
- **Popularity-aware with discovery** — Uses Deezer track rank, Last.fm listener counts, and MusicBrainz community ratings for popularity scoring (Deezer and MusicBrainz require no API key). Pass 2 is instructed to mix well-known and lesser-known artists rather than just picking the most popular names.
- **Mood & theme tagging** — Essentia audio analysis classifies tracks into a standardized vocabulary of 31 mood tags (happy, calm, dark, energetic, ...) and 26 theme tags (film, party, summer, ...) with confidence scores. The AI receives the full vocabulary in Pass 1 to map natural language prompts to precise database filters.
- **Negative filters** — "NOT", "no", "without" in prompts automatically exclude matching genres, artists, or keywords
- **Artist diversity** — Candidates are capped at 30% of requested song count per artist (min 3) so one artist never dominates; cap is skipped when specific artists are requested
- **Smart filter relaxation** — When strict filters (mood, BPM, keywords) return too few matches, filters are progressively relaxed rather than thrown away entirely, preserving genre and year context as long as possible
- **Real-time progress** — SSE streaming shows each generation phase as it happens with elapsed time
- **Multiple AI providers** — Claude (Anthropic) or Gemini (Google); switch per-request in the UI when both keys are configured
- **Rich metadata** — Scans BPM, mood, composer, label directly from audio files (richer than the Subsonic API)
- **Multiple media servers** — Save playlists to Navidrome, Plex/Plexamp, or both. Toggle between servers in the UI when both are configured.
- **Export options** — Save to your media server or download as .m3u
- **Navidrome playlist watcher** — Create an empty playlist named `"your prompt [navicraft]"` in Navidrome (or any Subsonic client) and NaviCraft auto-generates and populates it. Supports `duration:` and `songs:` parameters. No need to open the NaviCraft UI at all.
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
| Timezone | `UTC` | IANA timezone for schedule window (e.g. `America/New_York`) |
| Mood Scan Enabled | `false` | Enable Essentia-based mood/theme tagging |
| Mood Scan Batch Size | `50` | Number of tracks to process per mood scan run |
| Mood Scan From Hour | `0` (midnight) | Schedule window start hour (0–23) |
| Mood Scan To Hour | `6` (6 AM) | Schedule window end hour (0–23) |
| Playlist Watcher Enabled | `false` | Enable Navidrome `[navicraft]` playlist detection |
| Playlist Watcher Interval | `30` seconds | How often to poll Navidrome for new `[navicraft]` playlists (10–300s) |

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

Mood/theme tagging uses **Essentia audio analysis only** (MTG-Jamendo model) to ensure a standardized, consistent vocabulary across your entire library. Tags are stored with confidence scores (e.g. `happy:0.85, energetic:0.72`). The raw file metadata `mood` field remains in the database but is not used for mood filtering.

**Setup:**

Docker users get Essentia automatically — the Docker image includes `essentia-tensorflow`. If you see the warning `essentia-tensorflow not available` in your logs, rebuild the image (`docker compose up -d --build`) to pick up the fix.

For local (non-Docker) development, `essentia-tensorflow` is included in `requirements.txt` but requires the `--pre` flag because it uses pre-release versioning:

```bash
pip install --pre -r requirements.txt
```

Or install it standalone:

```bash
pip install --pre essentia-tensorflow==2.1b6.dev1389
```

> **Note:** `essentia-tensorflow` provides pre-built wheels for Linux (x86_64) and macOS (x86_64/arm64) on Python 3.9–3.12. If no wheel is available for your platform, mood scanning will not be available — Essentia is required.

**How it works:**
- Enable in Settings under "Mood / Theme Tagging"
- Configure batch size and a schedule window (from/to hour in your timezone)
- Within the window (e.g. midnight–6 AM), batches run back-to-back automatically
- Use the play/pause button on the mood progress bar to run continuously outside the window
- On first run, Essentia models (~80MB) are downloaded automatically from `essentia.upf.edu`
- Tags are stored with confidence scores in separate `mood_tags` and `theme_tags` columns
- The full standardized vocabulary (57 tags) is provided to the AI in Pass 1 for accurate prompt-to-filter mapping
- Pass 2 receives tag names (without scores) merged into a single compact field to save tokens

**Troubleshooting:**
- **"essentia-tensorflow not available"** — The `--pre` flag was likely missing during install. Run `pip install --pre essentia-tensorflow==2.1b6.dev1389`. For Docker, rebuild the image to get the fixed Dockerfile.
- **"Essentia model download failed"** — The container needs internet access on first run to download models (~80MB). Check network/firewall settings.
- **Mood scanning is slow** — Audio analysis is CPU-heavy (~2-5s per track). Use a small batch size (e.g. 50) and let it run in the background over time.

## Navidrome Playlist Watcher

Generate AI playlists without ever leaving Navidrome. Create an empty playlist with a special name, and NaviCraft detects it, generates the playlist, populates it with songs, and renames it automatically.

### How to enable

1. Open NaviCraft Settings (gear icon)
2. Set **Playlist Watcher Enabled** to `true`
3. (Optional) Adjust the **Watcher Interval** (default: 30 seconds)
4. Make sure your Navidrome connection is configured and working (green dot in header)

Or set via environment variables:
```bash
NAVICRAFT_WATCHER_ENABLED=true
NAVICRAFT_WATCHER_INTERVAL=30
```

### How to use

In **any** Subsonic-compatible client (Navidrome web UI, Feishin, Symfonium, Aonsoku, play:Sub, etc.):

1. Create a new, empty playlist
2. Name it with your prompt followed by `[navicraft]`:
   ```
   chill jazz for studying [navicraft]
   ```
3. Wait ~30 seconds. NaviCraft will:
   - Detect the empty playlist with the `[navicraft]` tag
   - Run the full two-pass AI generation pipeline
   - Populate the playlist with songs from your library
   - Rename the playlist to an AI-chosen name (e.g. "Late Night Jazz Sessions")

### Playlist name format

```
<your prompt> [navicraft]
<your prompt> [navicraft, songs: <count>]
<your prompt> [navicraft, duration: <minutes>]
<your prompt> [navicraft, songs: <count>, duration: <minutes>]
```

| Parameter | Range | Default | Description |
|-----------|-------|---------|-------------|
| `songs` | 5–100 | 25 | Number of songs to generate |
| `duration` | 5–600 | — | Target playlist duration in minutes (overrides song count for sizing) |

### Examples

| Playlist name | What happens |
|---------------|-------------|
| `top love songs from the 80s [navicraft, duration: 90]` | ~90 minutes of 80s love songs |
| `energetic workout mix [navicraft, songs: 40]` | 40 high-energy tracks |
| `dinner party jazz [navicraft, duration: 120]` | ~2 hours of jazz |
| `best of Radiohead [navicraft]` | 25 songs (default), popularity-ranked |
| `90s hip hop NOT gangsta rap [navicraft, songs: 30]` | 30 songs, excludes gangsta rap |

### How it works (technical)

1. NaviCraft's background scheduler polls Navidrome every N seconds (configurable)
2. It calls `getPlaylists` via the Subsonic API and looks for playlists matching the `[navicraft, ...]` regex
3. Only **empty** playlists (songCount = 0) are processed — this prevents re-triggering
4. The prompt and parameters are parsed from the playlist name
5. The full two-pass AI generation pipeline runs (same as the web UI)
6. Matched Navidrome song IDs are added via the `updatePlaylist` Subsonic endpoint
7. The playlist is renamed to remove the `[navicraft]` tag
8. Processed playlist IDs are tracked in memory to prevent duplicates

### Troubleshooting

- **Playlist not being detected:** Make sure the watcher is enabled in Settings, the playlist is empty (0 songs), and the name contains `[navicraft]` (case-insensitive).
- **Playlist detected but no songs added:** Check NaviCraft logs (`docker logs navicraft | grep watcher`). Common causes: Navidrome IDs not synced (run a library scan), or library index is empty.
- **Watcher status:** Check `GET /api/watcher/status` for last check time, generation history, and errors.

---

## Navidrome WASM Plugin (Advanced)

For tighter in-server integration, NaviCraft includes a **Navidrome WASM plugin** boilerplate in the `navidrome-plugin/` directory. This plugin runs inside Navidrome itself (v0.60.0+) and delegates playlist generation to NaviCraft's backend API.

> **Note:** The playlist watcher above handles the same use case without any plugin installation. The WASM plugin is an alternative for users who prefer in-server execution or want to extend it further. Both approaches work independently — use one or the other, not both.

### Prerequisites

- **Navidrome v0.60.0+** with the plugin system enabled (`Plugins.Enabled = true`)
- **NaviCraft** running as a separate service (Docker container or standalone)
- **TinyGo** installed for building the plugin (`brew install tinygo` / `apt install tinygo`)
- **Go 1.22+** for dependency management

### Building the plugin

```bash
cd navidrome-plugin/

# Install Go dependencies
go mod tidy

# Build the WASM module
tinygo build -o plugin.wasm -target wasip1 -buildmode=c-shared .

# Package as .ndp file
zip -j navicraft.ndp manifest.json plugin.wasm

# Or use the Makefile shortcut:
make package
```

This produces `navicraft.ndp` — the plugin package file.

### Installing in Navidrome

1. **Locate your Navidrome plugins folder.** By default this is `$DataFolder/Plugins` (e.g., `/data/Plugins` in Docker). You can customize it with the `Plugins.Folder` config option.

2. **Copy the plugin:**
   ```bash
   # Docker example
   docker cp navicraft.ndp navidrome:/data/Plugins/navicraft.ndp

   # Or mount a host directory
   cp navicraft.ndp /path/to/navidrome/data/Plugins/
   ```

3. **Restart Navidrome** (or enable `Plugins.AutoReload = true` for hot-loading):
   ```bash
   docker restart navidrome
   ```

4. **Configure the plugin** in Navidrome's web UI:
   - Go to **Settings > Plugins** (admin only)
   - Find "NaviCraft" in the plugin list
   - Set the **NaviCraft URL** to your NaviCraft instance (e.g., `http://navicraft:8765` if on the same Docker network, or `http://192.168.1.100:8085` for host networking)
   - Adjust the **Poll Interval** and **Default Song Count** if desired
   - Enable the plugin

5. **Verify the connection** — check Navidrome logs for:
   ```
   NaviCraft plugin initialized. Backend URL: http://navicraft:8765
   NaviCraft backend connection verified
   ```

### Navidrome configuration options

These go in your Navidrome config file (`navidrome.toml`) or environment:

| Option | Default | Description |
|--------|---------|-------------|
| `Plugins.Enabled` | `true` | Enable the plugin system |
| `Plugins.Folder` | `$DataFolder/Plugins` | Directory containing `.ndp` files |
| `Plugins.AutoReload` | `false` | Auto-detect new/changed plugins without restart |
| `Plugins.CacheSize` | `200MB` | Compiled WASM module cache size |

### Plugin permissions

The NaviCraft plugin requests these permissions (declared in `manifest.json`):

| Permission | Reason |
|------------|--------|
| `http` | Communicate with NaviCraft backend API |
| `cache` | Track in-flight generation requests |
| `kvstore` | Persist generation history across restarts |
| `scheduler` | Poll for new `[navicraft]` playlists |
| `subsonicapi` | Read and update playlists |

### Docker networking for plugin

When both Navidrome and NaviCraft run as Docker containers, they need to communicate. Options:

**Same Docker network (recommended):**
```yaml
# docker-compose.yml
services:
  navidrome:
    # ... your existing navidrome config
    networks:
      - music

  navicraft:
    # ... your existing navicraft config
    networks:
      - music

networks:
  music:
    driver: bridge
```
Then set the plugin's NaviCraft URL to `http://navicraft:8765`.

**Host networking:**
Use your machine's IP address: `http://192.168.1.100:8085`.

### Extending the plugin

The plugin source code in `navidrome-plugin/main.go` implements:

| Export | Capability | Purpose |
|--------|-----------|---------|
| `nd_on_init` | LifecycleManagement | Validates NaviCraft connection on startup |
| `nd_scheduler_callback` | SchedulerCallback | Polls for `[navicraft]` playlists and triggers generation |

The host service wrappers (`getPlaylistsViaHost`, `updatePlaylistViaHost`) are placeholder implementations. Replace them with actual Navidrome plugin SDK host function calls based on your Navidrome version. See the [Navidrome plugin documentation](https://www.navidrome.org/docs/usage/features/plugins/) for the latest SDK reference.

Potential extensions:
- **MetadataAgent** — Expose NaviCraft's mood/popularity data to Navidrome's library view
- **Lyrics** — Integrate with NaviCraft's metadata for richer track information
- **TaskWorker** — Run generation as a background task with progress tracking

---

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
│   ├── main.py              # FastAPI routes, SSE streaming, rate limiting
│   ├── config.py            # Config with JSON persistence + env var fallbacks
│   ├── database.py          # SQLite schema, queries, migrations
│   ├── scanner.py           # mutagen-based file scanner
│   ├── ai_engine.py         # Two-pass AI (Claude / Gemini)
│   ├── navidrome.py         # Subsonic API client (playlist CRUD + ID sync)
│   ├── plex.py              # Plex HTTP API client
│   ├── popularity.py        # Deezer + Last.fm + MusicBrainz enrichment
│   ├── mood_scanner.py      # Essentia-only mood/theme tagging with confidence scores
│   ├── playlist_watcher.py  # Navidrome [navicraft] playlist detection + generation
│   ├── scheduler.py         # Background scan + enrichment + mood scan + watcher jobs
│   └── requirements.txt
├── frontend/
│   ├── index.html           # SPA markup (no build step)
│   └── assets/
│       ├── app.js           # Frontend logic
│       └── styles.css       # Styles
├── navidrome-plugin/
│   ├── manifest.json        # Navidrome WASM plugin manifest
│   ├── main.go              # Plugin source (Go/TinyGo)
│   ├── go.mod               # Go module definition
│   └── Makefile             # Build + package commands
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
| POST | `/api/mood/continuous` | Start/stop continuous mood scanning (play/pause) |
| GET | `/api/watcher/status` | Playlist watcher status and generation history |
| POST | `/api/plugin/generate` | Synchronous playlist generation for plugins (no SSE) |

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
- **Deezer and MusicBrainz work out of the box.** No API keys needed. Add a Last.fm key for even better popularity data.
- **Use negative filters.** "Jazz but NOT smooth jazz" or "Electronic without EDM" works — the AI extracts exclusions and applies them at the SQL query stage.
- **Keywords work too.** Prompts like "greatest hits" or "songs about love" extract keywords that match against song titles, albums, and comments.
- **Claude vs Gemini:** Claude tends to produce more thoughtful, ordered playlists. Gemini is faster and has a generous free tier. Both can be active simultaneously and switched per-request in the UI.
- **Large libraries (50k+):** The two-pass strategy handles this well. If the AI misses songs you'd expect, increase `MAX_CANDIDATES` (uses more tokens per request).
- **Media server ID sync:** NaviCraft matches songs to Navidrome/Plex by file path. If paths differ (e.g. symlinks), it falls back to artist + title matching. When both servers are configured, IDs are synced independently for each.
- **Manual rescan:** Click the ♪ logo mark in the top-left to trigger an incremental rescan at any time.

## License

MIT
