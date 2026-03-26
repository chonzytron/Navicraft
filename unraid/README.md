# NaviCraft on Unraid

## Quick Start (User Script)

The simplest way to deploy NaviCraft on Unraid:

1. Install the **User Scripts** plugin from Community Applications
2. Go to **Settings > User Scripts > Add New Script**
3. Name it `deploy-navicraft`
4. Click the gear icon and paste the contents of `deploy-navicraft.sh`
5. **Edit the configuration section** at the top of the script:
   - Set `NAVIDROME_URL` to your Navidrome address (e.g., `http://192.168.1.100:4533`)
   - Set `NAVIDROME_USER` and `NAVIDROME_PASSWORD`
   - Set your `AI_PROVIDER` and API key (`CLAUDE_API_KEY` or `GEMINI_API_KEY`)
   - Adjust `MUSIC_PATH` if your music isn't at `/mnt/user/media/music`
6. Click **Run Script**
7. Access NaviCraft at `http://[YOUR_UNRAID_IP]:8085`

### Run at Array Start

To auto-start NaviCraft when the array comes online, set the script schedule to **At Startup of Array**.

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

> **Note:** The XML template requires the Docker image to be published at `ghcr.io/chonzytron/navicraft:latest`. Use the user script with `BUILD_FROM_SOURCE=true` if the image isn't available yet.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MUSIC_PATH` | `/mnt/user/media/music` | Host path to your music library |
| `NAVIDROME_URL` | `http://192.168.1.100:4533` | Navidrome URL |
| `NAVIDROME_USER` | `admin` | Navidrome username |
| `NAVIDROME_PASSWORD` | — | Navidrome password |
| `AI_PROVIDER` | `claude` | `claude` or `gemini` |
| `CLAUDE_API_KEY` | — | Anthropic API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `WEB_PORT` | `8085` | Web UI port |
| `SCAN_INTERVAL_HOURS` | `6` | Auto-scan interval |

## Networking

- **Bridge mode (default):** Use your Unraid IP for `NAVIDROME_URL` (e.g., `http://192.168.1.100:4533`)
- **Custom network:** If both containers are on the same Docker network, use the container name (e.g., `http://navidrome:4533`)

## Logs

```bash
docker logs -f navicraft
```

## Updating

Re-run the user script — it will stop the old container, pull the latest image, and start a new one.
