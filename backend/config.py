import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Music library path (mounted volume)
    music_dir: str = field(default_factory=lambda: os.getenv("MUSIC_DIR", "/music"))

    # Navidrome / Subsonic API (used only for playlist creation + ID sync)
    navidrome_url: str = field(default_factory=lambda: os.getenv("NAVIDROME_URL", "http://localhost:4533"))
    navidrome_user: str = field(default_factory=lambda: os.getenv("NAVIDROME_USER", "admin"))
    navidrome_password: str = field(default_factory=lambda: os.getenv("NAVIDROME_PASSWORD", ""))

    # AI Provider: "claude" or "gemini"
    ai_provider: str = field(default_factory=lambda: os.getenv("AI_PROVIDER", "claude"))
    claude_api_key: str = field(default_factory=lambda: os.getenv("CLAUDE_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))

    # Database
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "/data/navicraft.db"))

    # Scanner settings
    scan_interval_hours: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_HOURS", "6")))
    scan_extensions: str = field(default_factory=lambda: os.getenv(
        "SCAN_EXTENSIONS", ".mp3,.flac,.ogg,.opus,.m4a,.wma,.aac,.wav,.aiff,.ape,.wv,.mpc"
    ))

    # AI settings
    max_candidates: int = field(default_factory=lambda: int(os.getenv("MAX_CANDIDATES", "500")))

    @property
    def extensions_set(self) -> set:
        return {e.strip().lower() for e in self.scan_extensions.split(",")}


config = Config()
