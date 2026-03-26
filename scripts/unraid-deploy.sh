#!/bin/bash
#
# NaviCraft — Deploy / Update from GitHub
# ----------------------------------------
# Unraid User Script: install via Settings > User Scripts > Add New Script
# Schedule: run manually, or set a cron for auto-updates
#
# First run: clones the repo, creates .env from template, builds and starts
# Subsequent runs: pulls latest changes, rebuilds only if needed, restarts
#

# === CONFIGURATION — edit these ===
GITHUB_REPO="YOUR_GITHUB_USERNAME/navicraft"   # ← change to your repo
INSTALL_DIR="/mnt/user/appdata/navicraft"
MUSIC_PATH="/mnt/user/media/music"              # ← your music library path
NAVICRAFT_PORT="8085"

# === END CONFIGURATION ===

set -e

echo "========================================"
echo " NaviCraft Deploy/Update"
echo " $(date)"
echo "========================================"

# Install git if missing (Unraid doesn't always have it)
if ! command -v git &> /dev/null; then
    echo "ERROR: git not found. Install the 'Nerd Tools' plugin from Community Apps and enable git."
    exit 1
fi

# Install docker-compose if only docker compose (v2) is available
COMPOSE_CMD="docker compose"
if ! docker compose version &> /dev/null 2>&1; then
    if command -v docker-compose &> /dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        echo "ERROR: Neither 'docker compose' nor 'docker-compose' found."
        exit 1
    fi
fi

# --- Clone or Pull ---
if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "→ First run: cloning repository..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "https://github.com/${GITHUB_REPO}.git" "$INSTALL_DIR"
    cd "$INSTALL_DIR"

    # Create .env from template
    if [ ! -f .env ]; then
        cp .env.example .env
        echo ""
        echo "================================================"
        echo " IMPORTANT: Edit your .env file before starting!"
        echo " ${INSTALL_DIR}/.env"
        echo ""
        echo " At minimum, set:"
        echo "   NAVIDROME_URL"
        echo "   NAVIDROME_PASSWORD"
        echo "   CLAUDE_API_KEY (or GEMINI_API_KEY)"
        echo "================================================"
        echo ""

        # Inject the music path and port into .env
        sed -i "s|^MUSIC_DIR=.*|MUSIC_DIR=/music|" .env
    fi
else
    echo "→ Pulling latest changes..."
    cd "$INSTALL_DIR"
    
    # Stash any local changes to avoid conflicts
    git stash --quiet 2>/dev/null || true
    
    BEFORE=$(git rev-parse HEAD)
    git pull --ff-only origin main
    AFTER=$(git rev-parse HEAD)

    if [ "$BEFORE" = "$AFTER" ]; then
        echo "  Already up to date."
    else
        echo "  Updated: $(git log --oneline ${BEFORE}..${AFTER} | wc -l) new commits"
        git log --oneline "${BEFORE}..${AFTER}" | head -10
    fi
fi

# --- Export vars for docker-compose ---
export MUSIC_PATH="$MUSIC_PATH"
export NAVICRAFT_PORT="$NAVICRAFT_PORT"

# --- Build and start ---
echo ""
echo "→ Building and starting NaviCraft..."
$COMPOSE_CMD up -d --build --remove-orphans

echo ""
echo "→ Cleaning up old images..."
docker image prune -f --filter "dangling=true" 2>/dev/null || true

echo ""
echo "========================================"
echo " NaviCraft is running at:"
echo " http://$(hostname -I | awk '{print $1}'):${NAVICRAFT_PORT}"
echo "========================================"

# Show container status
echo ""
docker ps --filter "name=navicraft" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
