#!/bin/bash
#
# NaviCraft Unraid User Script
# =============================
# Deploy NaviCraft as a Docker container on Unraid.
#
# Usage:
#   1. Install "User Scripts" plugin from Community Applications
#   2. Add a new script, paste this content
#   3. Edit the configuration variables below
#   4. Run the script (or schedule it at array start)
#

set -e

# =============================================================================
# CONFIGURATION — Edit these to match your setup
# =============================================================================

# Container name
CONTAINER_NAME="navicraft"

# Port for the web UI
WEB_PORT="8085"

# Path to your music library (same as Navidrome)
MUSIC_PATH="/mnt/user/media/music"

# Path for NaviCraft persistent data (SQLite DB + config)
APPDATA_PATH="/mnt/user/appdata/navicraft"

# Scanner file extensions (which audio formats to index)
SCAN_EXTENSIONS=".mp3,.flac,.ogg,.opus,.m4a,.wma,.aac,.wav,.aiff,.ape,.wv,.mpc"

# Max candidate tracks sent to AI Pass 2
MAX_CANDIDATES="500"

# Docker image (use ghcr.io for pre-built, or build locally)
DOCKER_IMAGE="ghcr.io/chonzytron/navicraft:latest"

# Set to "true" to build from source instead of pulling the image.
# Building from source includes essentia-tensorflow for mood/theme audio analysis.
# If mood scanning logs "essentia-tensorflow not installed", rebuild to pick up the fix.
BUILD_FROM_SOURCE="false"
SOURCE_PATH="/mnt/user/appdata/navicraft/source"

# ─────────────────────────────────────────────────────────────────────────────
# NOTE: Navidrome, Plex, AI provider/keys/models, Last.fm API key,
# scan interval, and mood/theme tagging settings are now configurable
# from the Settings gear icon in the web UI. Those settings persist
# in /data/navicraft_config.json.
#
# You can still pass them as env vars below for initial bootstrap or
# headless deployments. Env vars act as defaults — UI settings override them.
# ─────────────────────────────────────────────────────────────────────────────

# Uncomment and set any of these to pre-configure via env vars:
# NAVIDROME_URL="http://192.168.1.100:4533"
# NAVIDROME_USER="admin"
# NAVIDROME_PASSWORD="your_password_here"
# PLEX_URL=""
# PLEX_TOKEN=""
# AI_PROVIDER="claude"
# CLAUDE_API_KEY=""
# CLAUDE_MODEL="claude-3-5-sonnet-20241022"
# GEMINI_API_KEY=""
# GEMINI_MODEL="gemini-2.5-flash"
# LASTFM_API_KEY=""
# SCAN_INTERVAL_HOURS="6"
# MOOD_SCAN_ENABLED="false"
# MOOD_SCAN_BATCH_SIZE="50"
# MOOD_SCAN_INTERVAL_HOURS="24"

# =============================================================================
# DEPLOYMENT — No need to edit below this line
# =============================================================================

echo "=========================================="
echo " NaviCraft Deployment Script"
echo "=========================================="

# Create appdata directory
mkdir -p "$APPDATA_PATH"

# Stop and remove existing container if running
if docker inspect "$CONTAINER_NAME" &>/dev/null; then
    echo "Stopping existing $CONTAINER_NAME container..."
    docker stop "$CONTAINER_NAME" 2>/dev/null || true
    docker rm "$CONTAINER_NAME" 2>/dev/null || true
fi

# Always pull the latest image (never reuse a cached version)
echo "Pulling latest NaviCraft image..."
docker pull "$DOCKER_IMAGE"

# Build from source if requested (overrides the pulled image)
if [ "$BUILD_FROM_SOURCE" = "true" ]; then
    echo "Building NaviCraft from source at $SOURCE_PATH..."
    if [ ! -d "$SOURCE_PATH" ]; then
        echo "ERROR: Source path $SOURCE_PATH does not exist."
        echo "Clone the repo first: git clone https://github.com/chonzytron/navicraft.git $SOURCE_PATH"
        exit 1
    fi
    docker build --no-cache -t navicraft:latest "$SOURCE_PATH"
    DOCKER_IMAGE="navicraft:latest"
fi

# Remove dangling images left behind by the pull/build above
echo "Cleaning up unused images..."
docker image prune -f

echo "Deploying NaviCraft..."
echo "  Image:    $DOCKER_IMAGE"
echo "  Port:     $WEB_PORT"
echo "  Music:    $MUSIC_PATH"
echo "  Data:     $APPDATA_PATH"
echo ""
echo "  Configure Navidrome, Plex, AI keys, etc. via the"
echo "  Settings gear icon in the web UI after first launch."
echo ""

# Build optional env var flags (only passed if the variable is set)
OPTIONAL_ENVS=()
[ -n "${NAVIDROME_URL:-}" ]        && OPTIONAL_ENVS+=(-e "NAVIDROME_URL=$NAVIDROME_URL")
[ -n "${NAVIDROME_USER:-}" ]       && OPTIONAL_ENVS+=(-e "NAVIDROME_USER=$NAVIDROME_USER")
[ -n "${NAVIDROME_PASSWORD:-}" ]   && OPTIONAL_ENVS+=(-e "NAVIDROME_PASSWORD=$NAVIDROME_PASSWORD")
[ -n "${PLEX_URL:-}" ]             && OPTIONAL_ENVS+=(-e "PLEX_URL=$PLEX_URL")
[ -n "${PLEX_TOKEN:-}" ]           && OPTIONAL_ENVS+=(-e "PLEX_TOKEN=$PLEX_TOKEN")
[ -n "${AI_PROVIDER:-}" ]          && OPTIONAL_ENVS+=(-e "AI_PROVIDER=$AI_PROVIDER")
[ -n "${CLAUDE_API_KEY:-}" ]       && OPTIONAL_ENVS+=(-e "CLAUDE_API_KEY=$CLAUDE_API_KEY")
[ -n "${CLAUDE_MODEL:-}" ]         && OPTIONAL_ENVS+=(-e "CLAUDE_MODEL=$CLAUDE_MODEL")
[ -n "${GEMINI_API_KEY:-}" ]       && OPTIONAL_ENVS+=(-e "GEMINI_API_KEY=$GEMINI_API_KEY")
[ -n "${GEMINI_MODEL:-}" ]         && OPTIONAL_ENVS+=(-e "GEMINI_MODEL=$GEMINI_MODEL")
[ -n "${LASTFM_API_KEY:-}" ]       && OPTIONAL_ENVS+=(-e "LASTFM_API_KEY=$LASTFM_API_KEY")
[ -n "${SCAN_INTERVAL_HOURS:-}" ]  && OPTIONAL_ENVS+=(-e "SCAN_INTERVAL_HOURS=$SCAN_INTERVAL_HOURS")
[ -n "${MOOD_SCAN_ENABLED:-}" ]   && OPTIONAL_ENVS+=(-e "MOOD_SCAN_ENABLED=$MOOD_SCAN_ENABLED")
[ -n "${MOOD_SCAN_BATCH_SIZE:-}" ] && OPTIONAL_ENVS+=(-e "MOOD_SCAN_BATCH_SIZE=$MOOD_SCAN_BATCH_SIZE")
[ -n "${MOOD_SCAN_INTERVAL_HOURS:-}" ] && OPTIONAL_ENVS+=(-e "MOOD_SCAN_INTERVAL_HOURS=$MOOD_SCAN_INTERVAL_HOURS")

docker run -d \
    --name="$CONTAINER_NAME" \
    --restart=unless-stopped \
    -p "$WEB_PORT:8085" \
    -v "$MUSIC_PATH:/music:ro" \
    -v "$APPDATA_PATH:/data:rw" \
    -e "MUSIC_DIR=/music" \
    -e "DB_PATH=/data/navicraft.db" \
    -e "SCAN_EXTENSIONS=$SCAN_EXTENSIONS" \
    -e "MAX_CANDIDATES=$MAX_CANDIDATES" \
    "${OPTIONAL_ENVS[@]}" \
    "$DOCKER_IMAGE"

echo ""
echo "=========================================="
echo " NaviCraft is running!"
echo " Web UI: http://$(hostname -I | awk '{print $1}'):$WEB_PORT"
echo "=========================================="
echo ""
echo "View logs: docker logs -f $CONTAINER_NAME"
