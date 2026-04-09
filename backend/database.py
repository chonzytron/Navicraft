"""
SQLite database for the local music index.
Stores rich metadata per track, supports filtering queries for the AI pipeline.
"""

import sqlite3
import os
import time
import logging
from contextlib import contextmanager
from typing import Optional
from config import config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT UNIQUE NOT NULL,
    file_mtime      REAL NOT NULL,
    file_size       INTEGER,
    format          TEXT,
    bitrate         INTEGER,
    sample_rate     INTEGER,
    channels        INTEGER,
    title           TEXT,
    artist          TEXT,
    album_artist    TEXT,
    album           TEXT,
    genre           TEXT,
    year            INTEGER,
    track_number    INTEGER,
    disc_number     INTEGER,
    duration        REAL,
    bpm             REAL,
    composer        TEXT,
    comment         TEXT,
    label           TEXT,
    mood            TEXT,
    navidrome_id    TEXT,
    plex_id         TEXT,
    popularity      INTEGER,
    lastfm_listeners INTEGER,
    lastfm_playcount INTEGER,
    deezer_rank     INTEGER,
    deezer_id       TEXT,
    deezer_checked_at REAL,
    lastfm_checked_at  REAL,
    musicbrainz_rating INTEGER,
    musicbrainz_rating_count INTEGER,
    musicbrainz_checked_at REAL,
    mood_tags        TEXT,
    theme_tags       TEXT,
    essentia_scanned_at REAL
);

CREATE INDEX IF NOT EXISTS idx_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_album ON tracks(album);
CREATE INDEX IF NOT EXISTS idx_genre ON tracks(genre);
CREATE INDEX IF NOT EXISTS idx_year ON tracks(year);
CREATE INDEX IF NOT EXISTS idx_mood ON tracks(mood);
CREATE INDEX IF NOT EXISTS idx_navidrome_id ON tracks(navidrome_id);

CREATE TABLE IF NOT EXISTS scan_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    tracks_scanned  INTEGER DEFAULT 0,
    tracks_added    INTEGER DEFAULT 0,
    tracks_updated  INTEGER DEFAULT 0,
    tracks_removed  INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

"""


def init_db():
    """Initialize the database and create tables.

    Migration runs BEFORE indexes so that indexes on new columns
    (e.g. plex_id) don't fail on databases created before those columns existed.
    """
    os.makedirs(os.path.dirname(config.db_path), exist_ok=True)
    with get_db() as conn:
        # Create tables first (skips if they already exist)
        conn.executescript(SCHEMA)
        # Add any missing columns from newer versions
        _migrate(conn)
        # Create indexes on migrated columns (safe now that columns exist)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plex_id ON tracks(plex_id)")
    logger.info("Database initialized at %s", config.db_path)


def _migrate(conn: sqlite3.Connection):
    """Add columns that may not exist in older databases."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()}
    migrations = [
        ("plex_id", "TEXT"),
        ("popularity", "INTEGER"),
        ("lastfm_listeners", "INTEGER"),
        ("lastfm_playcount", "INTEGER"),
        ("deezer_rank", "INTEGER"),
        ("deezer_id", "TEXT"),
        ("deezer_checked_at", "REAL"),
        ("lastfm_checked_at", "REAL"),
        ("musicbrainz_rating", "INTEGER"),
        ("musicbrainz_rating_count", "INTEGER"),
        ("musicbrainz_checked_at", "REAL"),
        ("mood_tags", "TEXT"),
        ("theme_tags", "TEXT"),
        ("essentia_scanned_at", "REAL"),
    ]
    for col, typ in migrations:
        if col not in columns:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {typ}")
            logger.info("Migrated: added column '%s' to tracks", col)


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(config.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Track operations ---

def upsert_track(db: sqlite3.Connection, track: dict):
    """Insert or update a track in the index."""
    db.execute("""
        INSERT INTO tracks (
            file_path, file_mtime, file_size, format, bitrate, sample_rate, channels,
            title, artist, album_artist, album, genre, year,
            track_number, disc_number, duration, bpm, composer, comment, label, mood
        ) VALUES (
            :file_path, :file_mtime, :file_size, :format, :bitrate, :sample_rate, :channels,
            :title, :artist, :album_artist, :album, :genre, :year,
            :track_number, :disc_number, :duration, :bpm, :composer, :comment, :label, :mood
        )
        ON CONFLICT(file_path) DO UPDATE SET
            file_mtime=:file_mtime, file_size=:file_size, format=:format,
            bitrate=:bitrate, sample_rate=:sample_rate, channels=:channels,
            title=:title, artist=:artist, album_artist=:album_artist, album=:album,
            genre=:genre, year=:year, track_number=:track_number, disc_number=:disc_number,
            duration=:duration, bpm=:bpm, composer=:composer, comment=:comment,
            label=:label, mood=:mood
    """, track)


def get_track_mtime(db: sqlite3.Connection, file_path: str) -> Optional[float]:
    """Get stored mtime for a file, or None if not indexed."""
    row = db.execute("SELECT file_mtime FROM tracks WHERE file_path = ?", (file_path,)).fetchone()
    return row["file_mtime"] if row else None


def get_all_paths(db: sqlite3.Connection) -> set:
    """Get all indexed file paths."""
    rows = db.execute("SELECT file_path FROM tracks").fetchall()
    return {r["file_path"] for r in rows}


def remove_tracks(db: sqlite3.Connection, paths: list):
    """Remove tracks by file path."""
    if not paths:
        return
    placeholders = ",".join("?" for _ in paths)
    db.execute(f"DELETE FROM tracks WHERE file_path IN ({placeholders})", paths)


def update_navidrome_id(db: sqlite3.Connection, file_path: str, navidrome_id: str):
    """Set the Navidrome ID for a track."""
    db.execute("UPDATE tracks SET navidrome_id = ? WHERE file_path = ?", (navidrome_id, file_path))


def bulk_update_navidrome_ids(db: sqlite3.Connection, mapping: dict):
    """Bulk update navidrome IDs. mapping = {file_path: navidrome_id}"""
    db.executemany(
        "UPDATE tracks SET navidrome_id = ? WHERE file_path = ?",
        [(nid, fp) for fp, nid in mapping.items()]
    )


def update_plex_id(db: sqlite3.Connection, file_path: str, plex_id: str):
    """Set the Plex rating key for a track."""
    db.execute("UPDATE tracks SET plex_id = ? WHERE file_path = ?", (plex_id, file_path))


def bulk_update_plex_ids(db: sqlite3.Connection, mapping: dict):
    """Bulk update Plex IDs. mapping = {file_path: plex_id}"""
    db.executemany(
        "UPDATE tracks SET plex_id = ? WHERE file_path = ?",
        [(pid, fp) for fp, pid in mapping.items()]
    )


# --- Query operations ---

def get_library_stats(db: sqlite3.Connection) -> dict:
    """Get library summary statistics."""
    row = db.execute("""
        SELECT
            COUNT(*) as song_count,
            COUNT(DISTINCT artist) as artist_count,
            COUNT(DISTINCT album) as album_count,
            COALESCE(MIN(year), 0) as min_year,
            COALESCE(MAX(year), 0) as max_year,
            COALESCE(SUM(duration), 0) as total_duration
        FROM tracks
        WHERE title IS NOT NULL
    """).fetchone()
    return dict(row)


def get_genres(db: sqlite3.Connection) -> list[dict]:
    """Get all genres with song counts."""
    rows = db.execute("""
        SELECT genre, COUNT(*) as count
        FROM tracks
        WHERE genre IS NOT NULL AND genre != ''
        GROUP BY genre
        ORDER BY count DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_top_artists(db: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Get top artists by track count."""
    rows = db.execute("""
        SELECT artist, COUNT(*) as count
        FROM tracks
        WHERE artist IS NOT NULL AND artist != ''
        GROUP BY artist
        ORDER BY count DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_year_range(db: sqlite3.Connection) -> dict:
    """Get min/max years in the library."""
    row = db.execute("""
        SELECT MIN(year) as min_year, MAX(year) as max_year
        FROM tracks WHERE year IS NOT NULL AND year > 0
    """).fetchone()
    return dict(row) if row else {"min_year": None, "max_year": None}


def filter_tracks(db: sqlite3.Connection, filters: dict, limit: int = 500,
                   max_songs: int | None = None) -> list[dict]:
    """
    Query tracks matching AI-generated filters.
    filters can include: genres, year_min, year_max, artists, moods, bpm_min, bpm_max,
                         exclude_genres, exclude_artists, exclude_keywords
    Per-artist diversity cap is applied as 30% of max_songs (min 3) when no
    specific artists are requested. Skipped entirely when artists are specified.
    """
    conditions = ["title IS NOT NULL"]
    params = []

    if filters.get("genres"):
        genres = filters["genres"]
        genre_clauses = " OR ".join("LOWER(genre) LIKE ?" for _ in genres)
        conditions.append(f"({genre_clauses})")
        params.extend(f"%{g.lower()}%" for g in genres)

    if filters.get("year_min"):
        conditions.append("year >= ?")
        params.append(filters["year_min"])

    if filters.get("year_max"):
        conditions.append("year <= ?")
        params.append(filters["year_max"])

    if filters.get("artists"):
        artists = filters["artists"]
        artist_clauses = " OR ".join("LOWER(artist) LIKE ?" for _ in artists)
        conditions.append(f"({artist_clauses})")
        params.extend(f"%{a.lower()}%" for a in artists)

    if filters.get("moods"):
        moods = filters["moods"]
        mood_clauses = " OR ".join(
            "(LOWER(mood_tags) LIKE ? OR LOWER(theme_tags) LIKE ?)"
            for _ in moods
        )
        conditions.append(f"({mood_clauses})")
        for m in moods:
            params.extend([f"%{m.lower()}%", f"%{m.lower()}%"])

    if filters.get("bpm_min"):
        conditions.append("bpm >= ?")
        params.append(filters["bpm_min"])

    if filters.get("bpm_max"):
        conditions.append("bpm <= ?")
        params.append(filters["bpm_max"])

    # Negative filters — exclude genres, artists, keywords
    if filters.get("exclude_genres"):
        for eg in filters["exclude_genres"]:
            conditions.append("LOWER(genre) NOT LIKE ?")
            params.append(f"%{eg.lower()}%")

    if filters.get("exclude_artists"):
        for ea in filters["exclude_artists"]:
            conditions.append("LOWER(artist) NOT LIKE ?")
            params.append(f"%{ea.lower()}%")

    if filters.get("exclude_keywords"):
        for ek in filters["exclude_keywords"]:
            conditions.append("LOWER(title) NOT LIKE ? AND LOWER(album) NOT LIKE ?")
            params.extend([f"%{ek.lower()}%", f"%{ek.lower()}%"])

    where = " AND ".join(conditions)

    # Fetch more than needed to allow per-artist capping
    fetch_limit = limit * 3
    params.append(fetch_limit)

    rows = db.execute(f"""
        SELECT id, title, artist, album_artist, album, genre, year,
               duration, bpm, composer, mood, navidrome_id, plex_id, file_path,
               popularity, mood_tags, theme_tags
        FROM tracks
        WHERE {where}
        ORDER BY (COALESCE(popularity, 50) * 0.6 + ABS(RANDOM()) % 50) DESC
        LIMIT ?
    """, params).fetchall()

    # Apply per-artist diversity cap (skipped when specific artists are requested)
    has_artist_filter = bool(filters.get("artists"))
    if has_artist_filter or max_songs is None:
        max_per_artist = None  # No cap
    else:
        max_per_artist = max(3, round(max_songs * 0.3))

    results = []
    artist_counts: dict[str, int] = {}
    for r in rows:
        d = dict(r)
        if max_per_artist is not None:
            artist_key = (d.get("artist") or "").lower().strip()
            count = artist_counts.get(artist_key, 0)
            if count >= max_per_artist:
                continue
            artist_counts[artist_key] = count + 1
        results.append(d)
        if len(results) >= limit:
            break

    return results


def get_tracks_by_ids(db: sqlite3.Connection, track_ids: list[int]) -> list[dict]:
    """Fetch full track info by internal IDs."""
    if not track_ids:
        return []
    placeholders = ",".join("?" for _ in track_ids)
    rows = db.execute(f"""
        SELECT * FROM tracks WHERE id IN ({placeholders})
    """, track_ids).fetchall()
    return [dict(r) for r in rows]


def search_tracks(db: sqlite3.Connection, query: str, limit: int = 50) -> list[dict]:
    """Simple text search across title, artist, album."""
    q = f"%{query}%"
    rows = db.execute("""
        SELECT id, title, artist, album, genre, year, duration, navidrome_id, plex_id
        FROM tracks
        WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
        LIMIT ?
    """, (q, q, q, limit)).fetchall()
    return [dict(r) for r in rows]


# --- Scan log ---

def create_scan_log(db: sqlite3.Connection) -> int:
    """Start a new scan log entry."""
    cursor = db.execute(
        "INSERT INTO scan_log (started_at) VALUES (datetime('now'))"
    )
    return cursor.lastrowid


def update_scan_log(db: sqlite3.Connection, log_id: int, **kwargs):
    """Update a scan log entry."""
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    db.execute(f"UPDATE scan_log SET {sets} WHERE id = ?", [*kwargs.values(), log_id])


def get_last_scan(db: sqlite3.Connection) -> Optional[dict]:
    """Get the most recent scan log."""
    row = db.execute(
        "SELECT * FROM scan_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# --- Popularity ---

def get_tracks_without_popularity(db: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Get tracks that haven't been enriched with popularity data yet."""
    rows = db.execute("""
        SELECT id, title, artist, album, track_number
        FROM tracks
        WHERE popularity IS NULL AND title IS NOT NULL
        ORDER BY id
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_tracks_missing_deezer(db: sqlite3.Connection, limit: int = 500) -> list[dict]:
    """Get tracks that have been enriched but are missing Deezer data and are due for a retry.
    Tracks checked in the last 24h with no result are skipped (not found / retry tomorrow)."""
    rows = db.execute("""
        SELECT id, title, artist, track_number,
               lastfm_listeners, lastfm_playcount,
               musicbrainz_rating, musicbrainz_rating_count
        FROM tracks
        WHERE popularity IS NOT NULL
          AND deezer_rank IS NULL
          AND title IS NOT NULL
          AND (deezer_checked_at IS NULL
               OR (unixepoch() - deezer_checked_at) > 86400)
        ORDER BY deezer_checked_at ASC NULLS FIRST, id ASC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_tracks_missing_deezer(db: sqlite3.Connection) -> int:
    """Count enriched tracks that still have no Deezer data and are due for a retry."""
    row = db.execute("""
        SELECT COUNT(*) as cnt FROM tracks
        WHERE popularity IS NOT NULL AND deezer_rank IS NULL AND title IS NOT NULL
          AND (deezer_checked_at IS NULL OR (unixepoch() - deezer_checked_at) > 86400)
    """).fetchone()
    return row["cnt"]


def update_deezer_not_found(db: sqlite3.Connection, track_ids: list[int]):
    """Mark tracks as checked on Deezer but not found. They won't be retried for 24h."""
    now = time.time()
    db.executemany(
        "UPDATE tracks SET deezer_checked_at = ? WHERE id = ?",
        [(now, tid) for tid in track_ids],
    )


def get_tracks_missing_lastfm(db: sqlite3.Connection, limit: int = 500) -> list[dict]:
    """Get tracks that have been enriched but are missing Last.fm data and are due for a retry.
    Tracks checked in the last 24h with no result are skipped (not found / retry tomorrow).
    Returns existing Deezer and MusicBrainz values so the score can be reblended."""
    rows = db.execute("""
        SELECT id, title, artist, track_number,
               deezer_rank, musicbrainz_rating, musicbrainz_rating_count
        FROM tracks
        WHERE popularity IS NOT NULL
          AND lastfm_listeners IS NULL
          AND title IS NOT NULL
          AND (lastfm_checked_at IS NULL
               OR (unixepoch() - lastfm_checked_at) > 86400)
        ORDER BY lastfm_checked_at ASC NULLS FIRST, id ASC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_tracks_missing_lastfm(db: sqlite3.Connection) -> int:
    """Count enriched tracks that still have no Last.fm data and are due for a retry."""
    row = db.execute("""
        SELECT COUNT(*) as cnt FROM tracks
        WHERE popularity IS NOT NULL AND lastfm_listeners IS NULL AND title IS NOT NULL
          AND (lastfm_checked_at IS NULL OR (unixepoch() - lastfm_checked_at) > 86400)
    """).fetchone()
    return row["cnt"]


def update_lastfm_not_found(db: sqlite3.Connection, track_ids: list[int]):
    """Mark tracks as checked on Last.fm but not found. They won't be retried for 24h."""
    now = time.time()
    db.executemany(
        "UPDATE tracks SET lastfm_checked_at = ? WHERE id = ?",
        [(now, tid) for tid in track_ids],
    )


def get_tracks_missing_musicbrainz(db: sqlite3.Connection, limit: int = 500) -> list[dict]:
    """Get tracks that have been enriched but are missing MusicBrainz data and are due for a retry.
    Tracks checked in the last 24h with no result are skipped."""
    rows = db.execute("""
        SELECT id, title, artist, track_number,
               deezer_rank, lastfm_listeners, lastfm_playcount
        FROM tracks
        WHERE popularity IS NOT NULL
          AND musicbrainz_rating IS NULL
          AND title IS NOT NULL
          AND (musicbrainz_checked_at IS NULL
               OR (unixepoch() - musicbrainz_checked_at) > 86400)
        ORDER BY musicbrainz_checked_at ASC NULLS FIRST, id ASC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_tracks_missing_musicbrainz(db: sqlite3.Connection) -> int:
    """Count enriched tracks that still have no MusicBrainz data and are due for a retry."""
    row = db.execute("""
        SELECT COUNT(*) as cnt FROM tracks
        WHERE popularity IS NOT NULL AND musicbrainz_rating IS NULL AND title IS NOT NULL
          AND (musicbrainz_checked_at IS NULL OR (unixepoch() - musicbrainz_checked_at) > 86400)
    """).fetchone()
    return row["cnt"]


def update_musicbrainz_not_found(db: sqlite3.Connection, track_ids: list[int]):
    """Mark tracks as checked on MusicBrainz but not found. They won't be retried for 24h."""
    now = time.time()
    db.executemany(
        "UPDATE tracks SET musicbrainz_checked_at = ? WHERE id = ?",
        [(now, tid) for tid in track_ids],
    )


def update_musicbrainz_popularity(db: sqlite3.Connection, rows: list[tuple]):
    """Patch MusicBrainz + reblended popularity for tracks.
    Each row: (popularity, musicbrainz_rating, musicbrainz_rating_count, musicbrainz_checked_at, track_id)
    """
    db.executemany("""
        UPDATE tracks SET popularity = ?, musicbrainz_rating = ?, musicbrainz_rating_count = ?,
            musicbrainz_checked_at = ? WHERE id = ?
    """, rows)


def update_lastfm_popularity(db: sqlite3.Connection, rows: list[tuple]):
    """Patch Last.fm + reblended popularity for tracks that already have other source data.
    Each row: (popularity, lastfm_listeners, lastfm_playcount, lastfm_checked_at, track_id)
    """
    db.executemany("""
        UPDATE tracks SET popularity = ?, lastfm_listeners = ?, lastfm_playcount = ?,
            lastfm_checked_at = ? WHERE id = ?
    """, rows)


def update_deezer_popularity(db: sqlite3.Connection, rows: list[tuple]):
    """Patch Deezer + reblended popularity for tracks that already have other source data.
    Each row: (popularity, deezer_rank, deezer_id, deezer_checked_at, track_id)
    """
    db.executemany("""
        UPDATE tracks SET popularity = ?, deezer_rank = ?, deezer_id = ?,
            deezer_checked_at = ? WHERE id = ?
    """, rows)


def execute_count(db: sqlite3.Connection, sql: str) -> int:
    """Execute a COUNT query and return the result."""
    row = db.execute(sql).fetchone()
    return row["cnt"]


def count_tracks_without_popularity(db: sqlite3.Connection) -> int:
    """Count tracks without popularity data."""
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM tracks WHERE popularity IS NULL AND title IS NOT NULL"
    ).fetchone()
    return row["cnt"]


def reset_popularity(db: sqlite3.Connection):
    """Reset all popularity scores so they can be re-enriched."""
    db.execute("""
        UPDATE tracks SET popularity = NULL,
               lastfm_listeners = NULL, lastfm_playcount = NULL,
               deezer_rank = NULL, deezer_id = NULL,
               deezer_checked_at = NULL, lastfm_checked_at = NULL,
               musicbrainz_rating = NULL, musicbrainz_rating_count = NULL,
               musicbrainz_checked_at = NULL
    """)
    count = db.execute("SELECT COUNT(*) as cnt FROM tracks WHERE title IS NOT NULL").fetchone()["cnt"]
    return count


def update_popularity(db: sqlite3.Connection, track_id: int, popularity: int,
                      lastfm_listeners: int | None = None,
                      lastfm_playcount: int | None = None,
                      deezer_rank: int | None = None):
    """Update popularity data for a single track."""
    db.execute("""
        UPDATE tracks
        SET popularity = ?,
            lastfm_listeners = ?, lastfm_playcount = ?, deezer_rank = ?
        WHERE id = ?
    """, (popularity, lastfm_listeners, lastfm_playcount, deezer_rank, track_id))


def bulk_update_popularity(db: sqlite3.Connection, rows: list[tuple]):
    """
    Batch-update popularity for multiple tracks in one transaction.
    Each row: (popularity, lastfm_listeners, lastfm_playcount,
               deezer_rank, deezer_id, deezer_checked_at,
               lastfm_checked_at, musicbrainz_rating,
               musicbrainz_rating_count, musicbrainz_checked_at,
               track_id)
    """
    db.executemany("""
        UPDATE tracks
        SET popularity = ?,
            lastfm_listeners = ?, lastfm_playcount = ?,
            deezer_rank = ?, deezer_id = ?,
            deezer_checked_at = ?, lastfm_checked_at = ?,
            musicbrainz_rating = ?, musicbrainz_rating_count = ?,
            musicbrainz_checked_at = ?
        WHERE id = ?
    """, rows)


# --- Mood / Theme Tags ---

def get_tracks_without_mood_scan(db: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Get tracks that haven't been scanned for mood/theme tags yet."""
    rows = db.execute("""
        SELECT id, title, artist, album, file_path, mood
        FROM tracks
        WHERE essentia_scanned_at IS NULL AND title IS NOT NULL
        ORDER BY id
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_tracks_without_mood_scan(db: sqlite3.Connection) -> int:
    """Count tracks not yet scanned for mood/theme tags."""
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM tracks WHERE essentia_scanned_at IS NULL AND title IS NOT NULL"
    ).fetchone()
    return row["cnt"]


def count_tracks_with_mood_tags(db: sqlite3.Connection) -> int:
    """Count tracks that have at least one mood or theme tag."""
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM tracks WHERE (mood_tags IS NOT NULL OR theme_tags IS NOT NULL) AND title IS NOT NULL"
    ).fetchone()
    return row["cnt"]


def bulk_update_mood_tags(db: sqlite3.Connection, rows: list[tuple]):
    """Batch-update mood/theme tags for multiple tracks.
    Each row: (mood_tags, theme_tags, essentia_scanned_at, track_id)
    """
    db.executemany("""
        UPDATE tracks SET mood_tags = ?, theme_tags = ?, essentia_scanned_at = ?
        WHERE id = ?
    """, rows)


def reset_mood_tags(db: sqlite3.Connection) -> int:
    """Reset all mood/theme tag data so tracks can be re-scanned."""
    db.execute("""
        UPDATE tracks SET mood_tags = NULL, theme_tags = NULL, essentia_scanned_at = NULL
    """)
    count = db.execute("SELECT COUNT(*) as cnt FROM tracks WHERE title IS NOT NULL").fetchone()["cnt"]
    return count


def _parse_scored_tags(tag_string: str) -> list[str]:
    """Parse a scored tag string like 'happy:0.85, energetic:0.72' into tag names.
    Also handles legacy format without scores (e.g. 'happy, energetic')."""
    tags = []
    for part in tag_string.split(", "):
        part = part.strip()
        if not part:
            continue
        # Strip confidence score if present (e.g. "happy:0.85" -> "happy")
        tag_name = part.split(":")[0].strip()
        if tag_name:
            tags.append(tag_name)
    return tags


def get_mood_tag_summary(db: sqlite3.Connection) -> list[dict]:
    """Get distinct mood tags with approximate counts.
    Parses comma-separated mood_tags column (with optional confidence scores) in Python."""
    rows = db.execute("""
        SELECT mood_tags FROM tracks
        WHERE mood_tags IS NOT NULL AND mood_tags != ''
    """).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for tag in _parse_scored_tags(r["mood_tags"]):
            counts[tag] = counts.get(tag, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: -x[1])]


def get_theme_tag_summary(db: sqlite3.Connection) -> list[dict]:
    """Get distinct theme tags with approximate counts."""
    rows = db.execute("""
        SELECT theme_tags FROM tracks
        WHERE theme_tags IS NOT NULL AND theme_tags != ''
    """).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for tag in _parse_scored_tags(r["theme_tags"]):
            counts[tag] = counts.get(tag, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: -x[1])]


# --- Health Check & Cleanup ---

def count_tracks_without_title(db: sqlite3.Connection) -> int:
    """Count tracks with NULL title (incomplete/failed scans)."""
    row = db.execute("SELECT COUNT(*) as cnt FROM tracks WHERE title IS NULL").fetchone()
    return row["cnt"]


def count_stale_enrichment(db: sqlite3.Connection) -> dict:
    """Count tracks that were checked by enrichment sources but returned no data
    and are older than 7 days (to avoid resetting recent 'not found' results).
    MusicBrainz is excluded: most tracks legitimately have no community rating,
    so resetting 'not found' results just wastes API calls at 1 req/s."""
    cutoff = time.time() - 604800  # 7 days
    deezer = db.execute(
        "SELECT COUNT(*) as cnt FROM tracks WHERE deezer_checked_at IS NOT NULL AND deezer_checked_at < ? AND deezer_rank IS NULL AND title IS NOT NULL",
        (cutoff,)
    ).fetchone()["cnt"]
    lastfm = db.execute(
        "SELECT COUNT(*) as cnt FROM tracks WHERE lastfm_checked_at IS NOT NULL AND lastfm_checked_at < ? AND lastfm_listeners IS NULL AND title IS NOT NULL",
        (cutoff,)
    ).fetchone()["cnt"]
    return {"deezer": deezer, "lastfm": lastfm, "musicbrainz": 0}


def remove_tracks_without_title(db: sqlite3.Connection) -> int:
    """Remove tracks with NULL title (incomplete/failed scans)."""
    cursor = db.execute("DELETE FROM tracks WHERE title IS NULL")
    return cursor.rowcount


def reset_stale_enrichment(db: sqlite3.Connection) -> dict:
    """Reset checked_at timestamps for tracks that were checked but got no data
    and are older than 7 days. Recent 'not found' results are preserved so
    the enrichment 24h cooldown isn't bypassed on every scan.
    MusicBrainz is excluded: most tracks legitimately have no community rating,
    so resetting 'not found' results just triggers thousands of redundant API
    calls at MusicBrainz's strict 1 req/s rate limit."""
    cutoff = time.time() - 604800  # 7 days
    d = db.execute(
        "UPDATE tracks SET deezer_checked_at = NULL WHERE deezer_checked_at IS NOT NULL AND deezer_checked_at < ? AND deezer_rank IS NULL",
        (cutoff,)
    ).rowcount
    l = db.execute(
        "UPDATE tracks SET lastfm_checked_at = NULL WHERE lastfm_checked_at IS NOT NULL AND lastfm_checked_at < ? AND lastfm_listeners IS NULL",
        (cutoff,)
    ).rowcount
    return {"deezer": d, "lastfm": l, "musicbrainz": 0}


def count_scan_logs(db: sqlite3.Connection) -> int:
    """Count total scan log entries."""
    row = db.execute("SELECT COUNT(*) as cnt FROM scan_log").fetchone()
    return row["cnt"]


def prune_scan_logs(db: sqlite3.Connection, keep: int = 50) -> int:
    """Remove old scan log entries, keeping the most recent `keep`."""
    cursor = db.execute(
        "DELETE FROM scan_log WHERE id NOT IN (SELECT id FROM scan_log ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    return cursor.rowcount


# --- Settings ---

def get_setting(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Get a persistent setting value."""
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db: sqlite3.Connection, key: str, value: str):
    """Persist a setting value."""
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


