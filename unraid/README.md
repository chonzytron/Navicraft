# NaviCraft on Unraid

## Quick Start (User Script — recommended)

1. Install the **User Scripts** plugin from Community Applications
2. Go to **Settings > User Scripts > Add New Script**
3. Name it `deploy-navicraft`
4. Click the gear icon and paste the contents of `deploy-navicraft.sh`
5. **Edit the configuration block** at the top of the script:
   - Set your media server — Navidrome (`NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`) and/or Plex (`PLEX_URL`, `PLEX_TOKEN`). You can configure one or both.
   - Set `AI_PROVIDER` and the matching API key (`CLAUDE_API_KEY` or `GEMINI_API_KEY`)
   - Adjust `MUSIC_PATH` if your music isn't at `/mnt/user/media/music`
6. Click **Run Script**
7. Access NaviCraft at `http://[YOUR_UNRAID_IP]:8085`

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

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_PATH` | `/mnt/user/media/music` | Host path to your music library (mounted read-only) |
| `APPDATA_PATH` | `/mnt/user/appdata/navicraft` | Host path for persistent data (SQLite DB) |
| `WEB_PORT` | `8085` | Web UI port |
| `NAVIDROME_URL` | `http://192.168.1.100:4533` | Navidrome URL (use your Unraid IP). Leave empty if using Plex only. |
| `NAVIDROME_USER` | `admin` | Navidrome username |
| `NAVIDROME_PASSWORD` | — | Navidrome password |
| `PLEX_URL` | — | Plex server URL (e.g. `http://192.168.1.100:32400`). Leave empty if using Navidrome only. |
| `PLEX_TOKEN` | — | Plex authentication token ([how to find](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| `AI_PROVIDER` | `claude` | `claude` or `gemini` |
| `CLAUDE_API_KEY` | — | Anthropic API key |
| `CLAUDE_MODEL` | `claude-3-5-sonnet-20241022` | Claude model |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model |
| `LASTFM_API_KEY` | — | Last.fm API key (free, improves popularity) |
| `SCAN_INTERVAL_HOURS` | `6` | How often to auto-scan the library |

## Networking

NaviCraft runs in Docker bridge mode by default. This means:

- **Use your Unraid server's IP** for `NAVIDROME_URL` and `PLEX_URL`, not `localhost` or `127.0.0.1` (those refer to inside the NaviCraft container, not the host)
- Navidrome example: `http://192.168.1.100:4533`
- Plex example: `http://192.168.1.100:32400`
- If both NaviCraft and your media server are on the **same custom Docker network**, you can use the container name instead (e.g., `http://navidrome:4533`)

Connection status for each configured server is shown in the NaviCraft header with a green/red dot. Click to retest. When both servers are configured, a toggle lets you choose which server to save playlists to.

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
- Verify `NAVIDROME_URL` uses your Unraid IP, not `localhost`
- Confirm Navidrome is running: `docker ps | grep navidrome`
- Test from the host: `curl http://192.168.1.100:4533/rest/ping?u=admin&p=pass&v=1.16.1&c=test&f=json`

**Plex shows red in the UI:**
- Verify `PLEX_URL` uses your Unraid IP and correct port (default 32400)
- Verify `PLEX_TOKEN` is correct (tokens can expire if you change your Plex password)
- Confirm Plex is running: `docker ps | grep plex`

**Library not scanning:**
- Check that `MUSIC_PATH` points to your actual music directory
- Verify the directory is readable: `ls /mnt/user/media/music`

**Popularity not enriching:**
- Check logs for API errors: `docker logs navicraft | grep -i deezer`
- Deezer rate limits are generous (50 req/5s); if hit, NaviCraft slows down automatically

**AI generation failing:**
- Check logs for the actual error: `docker logs navicraft | grep ERROR`
- For Claude: verify API key and account has credits at console.anthropic.com
- For Gemini: verify API key is valid
