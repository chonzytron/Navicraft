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

# Path for NaviCraft persistent data (SQLite DB)
APPDATA_PATH="/mnt/user/appdata/navicraft"

# Navidrome connection
# If Navidrome runs in bridge mode, use your Unraid IP (e.g., http://192.168.1.100:4533)
# If using a custom Docker network, you can use the container name (e.g., http://navidrome:4533)
NAVIDROME_URL="http://192.168.1.100:4533"
NAVIDROME_USER="admin"
NAVIDROME_PASSWORD="your_password_here"

# AI Provider: "claude" or "gemini"
AI_PROVIDER="claude"

# Claude (Anthropic) — required if AI_PROVIDER=claude
CLAUDE_API_KEY=""
CLAUDE_MODEL="claude-sonnet-4-20250514"

# Gemini (Google) — required if AI_PROVIDER=gemini
GEMINI_API_KEY=""
GEMINI_MODEL="gemini-2.0-flash"

# Scanner settings
SCAN_INTERVAL_HOURS="6"

# Docker image (use ghcr.io for pre-built, or build locally)
DOCKER_IMAGE="ghcr.io/chonzytron/navicraft:latest"

# Set to "true" to build from source instead of pulling the image
BUILD_FROM_SOURCE="false"
SOURCE_PATH="/mnt/user/appdata/navicraft/source"

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

# Build from source if requested
if [ "$BUILD_FROM_SOURCE" = "true" ]; then
    echo "Building NaviCraft from source at $SOURCE_PATH..."
    if [ ! -d "$SOURCE_PATH" ]; then
        echo "ERROR: Source path $SOURCE_PATH does not exist."
        echo "Clone the repo first: git clone https://github.com/chonzytron/navicraft.git $SOURCE_PATH"
        exit 1
    fi
    docker build -t navicraft:latest "$SOURCE_PATH"
    DOCKER_IMAGE="navicraft:latest"
fi

echo "Deploying NaviCraft..."
echo "  Image:    $DOCKER_IMAGE"
echo "  Port:     $WEB_PORT"
echo "  Music:    $MUSIC_PATH"
echo "  Data:     $APPDATA_PATH"
echo "  AI:       $AI_PROVIDER"
echo ""

docker run -d \
    --name="$CONTAINER_NAME" \
    --restart=unless-stopped \
    -p "$WEB_PORT:8085" \
    -v "$MUSIC_PATH:/music:ro" \
    -v "$APPDATA_PATH:/data:rw" \
    -e "MUSIC_DIR=/music" \
    -e "DB_PATH=/data/navicraft.db" \
    -e "NAVIDROME_URL=$NAVIDROME_URL" \
    -e "NAVIDROME_USER=$NAVIDROME_USER" \
    -e "NAVIDROME_PASSWORD=$NAVIDROME_PASSWORD" \
    -e "AI_PROVIDER=$AI_PROVIDER" \
    -e "CLAUDE_API_KEY=$CLAUDE_API_KEY" \
    -e "CLAUDE_MODEL=$CLAUDE_MODEL" \
    -e "GEMINI_API_KEY=$GEMINI_API_KEY" \
    -e "GEMINI_MODEL=$GEMINI_MODEL" \
    -e "SCAN_INTERVAL_HOURS=$SCAN_INTERVAL_HOURS" \
    -e "SCAN_EXTENSIONS=.mp3,.flac,.ogg,.opus,.m4a,.wma,.aac,.wav,.aiff,.ape,.wv,.mpc" \
    -e "MAX_CANDIDATES=500" \
    "$DOCKER_IMAGE"

echo ""
echo "=========================================="
echo " NaviCraft is running!"
echo " Web UI: http://$(hostname -I | awk '{print $1}'):$WEB_PORT"
echo "=========================================="
echo ""
echo "View logs: docker logs -f $CONTAINER_NAME"
