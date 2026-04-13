import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("navicraft.config")

# Fields that can be edited via the UI config panel
EDITABLE_FIELDS = {
    "navidrome_url", "navidrome_user", "navidrome_password",
    "plex_url", "plex_token",
    "ai_provider", "claude_api_key", "claude_model",
    "gemini_api_key", "gemini_model",
    "lastfm_api_key", "scan_interval_hours",
    "timezone",
    "mood_scan_enabled",
    "mood_scan_from_hour", "mood_scan_to_hour",
    "navicraft_watcher_enabled", "navicraft_watcher_interval",
}

# Fields that contain secrets (masked in GET responses)
SECRET_FIELDS = {
    "navidrome_password", "plex_token",
    "claude_api_key", "gemini_api_key", "lastfm_api_key",
}


def _config_file_path() -> Path:
    data_dir = Path(os.getenv("DATA_DIR", os.path.dirname(os.getenv("DB_PATH", "/data/navicraft.db"))))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "navicraft_config.json"


def _load_config_overrides() -> dict:
    path = _config_file_path()
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.warning("Failed to read config file %s, using defaults", path)
    return {}


def _save_config_overrides(overrides: dict):
    path = _config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(overrides, f, indent=2)
    logger.info("Config saved to %s", path)


def _resolve(env_key: str, default: str, overrides: dict, field_name: str) -> str:
    """Config file overrides env vars, env vars override defaults."""
    if field_name in overrides and overrides[field_name] != "":
        return overrides[field_name]
    return os.getenv(env_key, default)


@dataclass
class Config:
    # Music library path (mounted volume)
    music_dir: str = field(default_factory=lambda: os.getenv("MUSIC_DIR", "/music"))

    # Navidrome / Subsonic API (used only for playlist creation + ID sync)
    navidrome_url: str = ""
    navidrome_user: str = ""
    navidrome_password: str = ""

    # Plex / Plexamp (used only for playlist creation + ID sync)
    plex_url: str = ""
    plex_token: str = ""

    # AI Provider: "claude" or "gemini"
    ai_provider: str = ""
    claude_api_key: str = ""
    claude_model: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""

    # Database
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "/data/navicraft.db"))

    # Scanner settings
    scan_interval_hours: int = 6
    scan_extensions: str = field(default_factory=lambda: os.getenv(
        "SCAN_EXTENSIONS", ".mp3,.flac,.ogg,.opus,.m4a,.wma,.aac,.wav,.aiff,.ape,.wv,.mpc"
    ))

    # Last.fm API (optional — for popularity enrichment)
    lastfm_api_key: str = ""

    # Timezone (IANA tz name, e.g. "America/New_York")
    timezone: str = "UTC"

    # Mood / theme tag scanning (Essentia + API)
    mood_scan_enabled: bool = False
    mood_scan_batch_size: int = 50
    mood_scan_from_hour: int = 0   # Schedule window start (0-23)
    mood_scan_to_hour: int = 6     # Schedule window end (0-23)

    # Navidrome playlist watcher (detects [navicraft, ...] playlists)
    navicraft_watcher_enabled: bool = False
    navicraft_watcher_interval: int = 30  # seconds between polls

    # AI settings
    max_candidates: int = field(default_factory=lambda: int(os.getenv("MAX_CANDIDATES", "500")))

    @property
    def extensions_set(self) -> set:
        return {e.strip().lower() for e in self.scan_extensions.split(",")}

    def reload_from_file(self):
        """Reload editable fields from the config JSON file (with env var fallbacks)."""
        overrides = _load_config_overrides()
        self.navidrome_url = _resolve("NAVIDROME_URL", "http://localhost:4533", overrides, "navidrome_url")
        self.navidrome_user = _resolve("NAVIDROME_USER", "admin", overrides, "navidrome_user")
        self.navidrome_password = _resolve("NAVIDROME_PASSWORD", "", overrides, "navidrome_password")
        self.plex_url = _resolve("PLEX_URL", "", overrides, "plex_url")
        self.plex_token = _resolve("PLEX_TOKEN", "", overrides, "plex_token")
        self.ai_provider = _resolve("AI_PROVIDER", "claude", overrides, "ai_provider")
        self.claude_api_key = _resolve("CLAUDE_API_KEY", "", overrides, "claude_api_key")
        self.claude_model = _resolve("CLAUDE_MODEL", "claude-3-5-sonnet-20241022", overrides, "claude_model")
        self.gemini_api_key = _resolve("GEMINI_API_KEY", "", overrides, "gemini_api_key")
        self.gemini_model = _resolve("GEMINI_MODEL", "gemini-2.5-flash", overrides, "gemini_model")
        self.lastfm_api_key = _resolve("LASTFM_API_KEY", "", overrides, "lastfm_api_key")
        raw_interval = _resolve("SCAN_INTERVAL_HOURS", "6", overrides, "scan_interval_hours")
        try:
            self.scan_interval_hours = int(raw_interval)
        except (ValueError, TypeError):
            self.scan_interval_hours = 6

        raw_tz = _resolve("TIMEZONE", "UTC", overrides, "timezone")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(raw_tz)
            self.timezone = raw_tz
        except Exception:
            self.timezone = "UTC"

        raw_mood_enabled = _resolve("MOOD_SCAN_ENABLED", "false", overrides, "mood_scan_enabled")
        self.mood_scan_enabled = raw_mood_enabled.lower() in ("true", "1", "yes")
        raw_from = _resolve("MOOD_SCAN_FROM_HOUR", "0", overrides, "mood_scan_from_hour")
        try:
            self.mood_scan_from_hour = max(0, min(23, int(raw_from)))
        except (ValueError, TypeError):
            self.mood_scan_from_hour = 0
        raw_to = _resolve("MOOD_SCAN_TO_HOUR", "6", overrides, "mood_scan_to_hour")
        try:
            self.mood_scan_to_hour = max(0, min(23, int(raw_to)))
        except (ValueError, TypeError):
            self.mood_scan_to_hour = 6

        raw_watcher = _resolve("NAVICRAFT_WATCHER_ENABLED", "false", overrides, "navicraft_watcher_enabled")
        self.navicraft_watcher_enabled = raw_watcher.lower() in ("true", "1", "yes")
        raw_interval = _resolve("NAVICRAFT_WATCHER_INTERVAL", "30", overrides, "navicraft_watcher_interval")
        try:
            self.navicraft_watcher_interval = max(10, min(300, int(raw_interval)))
        except (ValueError, TypeError):
            self.navicraft_watcher_interval = 30

    def get_editable(self) -> dict:
        """Return editable config values, masking secrets."""
        result = {}
        for f in EDITABLE_FIELDS:
            val = getattr(self, f, "")
            if f in SECRET_FIELDS and val:
                result[f] = val[:4] + "••••" + val[-4:] if len(str(val)) > 8 else "••••"
            elif isinstance(val, bool):
                result[f] = "true" if val else "false"
            else:
                result[f] = str(val) if not isinstance(val, str) else val
        return result

    def update_from_dict(self, data: dict):
        """Update editable fields from a dict and persist to JSON file."""
        overrides = _load_config_overrides()
        for key, value in data.items():
            if key not in EDITABLE_FIELDS:
                continue
            # Skip masked values (user didn't change the secret)
            if key in SECRET_FIELDS and "••••" in str(value):
                continue
            overrides[key] = value
        _save_config_overrides(overrides)
        self.reload_from_file()


config = Config()
config.reload_from_file()
