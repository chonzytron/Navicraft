# NaviCraft on Unraid

## Quick Start (User Script — recommended)

1. Install the **User Scripts** plugin from Community Applications
2. Go to **Settings > User Scripts > Add New Script**
3. Name it `deploy-navicraft`
4. Click the gear icon and paste the contents of `deploy-navicraft.sh`
5. **Edit the configuration block** at the top of the script:
   - Adjust `MUSIC_PATH` if your music isn't at `/mnt/user/media/music`
   - Adjust `WEB_PORT` if 8085 is taken
6. Click **Run Script**
7. Access NaviCraft at `http://[YOUR_UNRAID_IP]:8085`
8. Click the **Settings gear icon** in the header to configure Navidrome/Plex connections, AI provider and API keys, Last.fm, scan interval, and mood/theme tagging

### Run at Array Start

Set the script schedule to **At Startup of Array** so NaviCraft starts automatically when the array comes online.

### Updating

Re-run the script. It will:
1. Stop and remove the existing container
2. Pull the latest image from the registry (never reuses cache)
3. Remove old dangling image layers to keep disk clean
4. Start a fresh container with your current configuration

## Alternative: Docker Compose

If you have the **Docker Compose Manager** plugin:

```bash
cd /mnt/user/appdata/navicraft
git clone https://github.com/chonzytron/navicraft.git .
cp .env.example .env
# Edit .env with your settings
docker compose up -d --build
```

## Alternative: Unraid Docker UI (XML Template)

1. Copy `my-navicraft.xml` to `/boot/config/plugins/dockerMan/templates-user/`
2. Go to **Docker > Add Container > Select Template > navicraft**
3. Fill in the fields and click **Apply**

> **Note:** The XML template requires the Docker image to be published at `ghcr.io/chonzytron/navicraft:latest`. Use the user script with `BUILD_FROM_SOURCE=true` to build locally if the image isn't available.

## Configuration Reference

Most settings (servers, AI keys, models, Last.fm, scan interval, mood/theme tagging) are configured from the **Settings gear icon** in the web UI after first launch. They persist to `/data/navicraft_config.json`.

### Deploy script variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_PATH` | `/mnt/user/media/music` | Host path to your music library (mounted read-only) |
| `APPDATA_PATH` | `/mnt/user/appdata/navicraft` | Host path for persistent data (SQLite DB + config) |
| `WEB_PORT` | `8085` | Web UI port |
| `SCAN_EXTENSIONS` | `.mp3,.flac,.ogg,...` | Audio file extensions to scan |
| `MAX_CANDIDATES` | `500` | Max songs passed to AI Pass 2 |

### UI-configurable settings

These are set from the Settings panel in the web UI. They can also be pre-set as env vars in the deploy script (uncomment them) for initial bootstrap — UI settings override env vars.

| Setting | Default | Description |
|---------|---------|-------------|
| Navidrome URL | — | Navidrome URL (use your Unraid IP, not `localhost`) |
| Navidrome User | `admin` | Navidrome username |
| Navidrome Password | — | Navidrome password |
| Plex URL | — | Plex server URL (e.g. `http://192.168.1.100:32400`) |
| Plex Token | — | Plex authentication token ([how to find](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| AI Provider | `claude` | `claude` or `gemini` |
| Claude API Key | — | Anthropic API key |
| Claude Model | `claude-3-5-sonnet-20241022` | Claude model |
| Gemini API Key | — | Google Gemini API key |
| Gemini Model | `gemini-2.5-flash` | Gemini model |
| Last.fm API Key | — | Last.fm API key (free, improves popularity) |
| Scan Interval | `6` hours | How often to auto-scan the library |
| Mood Scan Enabled | `false` | Enable Essentia-based mood/theme audio analysis |
| Mood Scan Batch Size | `50` | Tracks to process per mood scan run |
| Mood Scan Interval | `24` hours | Hours to wait between mood scan batches (starts after batch completes) |

## Networking

NaviCraft runs in Docker bridge mode by default. This means:

- **Use your Unraid server's IP** for `NAVIDROME_URL` and `PLEX_URL`, not `localhost` or `127.0.0.1` (those refer to inside the NaviCraft container, not the host)
- Navidrome example: `http://192.168.1.100:4533`
- Plex example: `http://192.168.1.100:32400`
- If both NaviCraft and your media server are on the **same custom Docker network**, you can use the container name instead (e.g., `http://navidrome:4533`)

Connection status for each configured server is shown in the NaviCraft header with a green/red dot. Click to retest. When both servers are configured, a toggle lets you choose which server to save playlists to.

> **Tip:** After updating server URLs in the Settings panel, click the status dots to retest the connections immediately.

## API Keys

| Service | Required? | Cost | Link |
|---------|-----------|------|------|
| Claude (Anthropic) | If using Claude | Pay-per-use (separate from Claude.ai subscription) | [console.anthropic.com](https://console.anthropic.com) |
| Gemini (Google) | If using Gemini | Free tier available | [aistudio.google.com](https://aistudio.google.com) |
| Plex Token | If using Plex | Free (part of Plex) | [Finding your token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| Deezer | Automatic | Free, no key needed | — |
| Last.fm | Optional | Free | [last.fm/api/account/create](https://www.last.fm/api/account/create) |

> **Claude API vs Claude.ai:** A Claude Pro/Team subscription does **not** grant API access. The API requires separate credits at console.anthropic.com. Gemini is a good free alternative.

## Logs

```bash
docker logs -f navicraft
```

## Troubleshooting

**Navidrome shows red in the UI:**
- Open Settings (gear icon) and verify the Navidrome URL uses your Unraid IP, not `localhost`
- Confirm Navidrome is running: `docker ps | grep navidrome`
- Test from the host: `curl http://192.168.1.100:4533/rest/ping?u=admin&p=pass&v=1.16.1&c=test&f=json`

**Plex shows red in the UI:**
- Open Settings (gear icon) and verify the Plex URL uses your Unraid IP and correct port (default 32400)
- Verify the Plex Token is correct (tokens can expire if you change your Plex password)
- Confirm Plex is running: `docker ps | grep plex`

**Library not scanning:**
- Check that `MUSIC_PATH` points to your actual music directory
- Verify the directory is readable: `ls /mnt/user/media/music`

**Popularity not enriching:**
- Check logs for API errors: `docker logs navicraft | grep -i deezer`
- Deezer rate limits are generous (50 req/5s); if hit, NaviCraft slows down automatically

**Mood scanning not working:**
- Ensure mood scanning is enabled in Settings (gear icon) under "Mood / Theme Tagging"
- If logs show `essentia-tensorflow not installed`, re-pull or rebuild the image to pick up the latest Dockerfile which fixes the package version:
  - Pre-built image: re-run the deploy script (it always pulls latest)
  - Build from source: `git pull` in the source directory, then re-run the deploy script with `BUILD_FROM_SOURCE=true`
- On first run, Essentia models (~80MB) auto-download — the container needs internet access. Check logs for download progress
- Mood scanning is CPU-heavy (~2-5s per track); large batches may take a while
- Check logs: `docker logs navicraft | grep -i mood`

**AI generation failing:**
- Check logs for the actual error: `docker logs navicraft | grep ERROR`
- Open Settings (gear icon) and verify the API key for your selected provider is correct
- For Claude: verify account has credits at console.anthropic.com
- For Gemini: verify API key is valid at aistudio.google.com
