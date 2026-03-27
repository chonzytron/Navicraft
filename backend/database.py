"""
SQLite database for the local music index.
Stores rich metadata per track, supports filtering queries for the AI pipeline.
"""

import sqlite3
import os
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
    popularity      INTEGER,
    mb_rating       REAL,
    mb_rating_count INTEGER DEFAULT 0,
    lastfm_listeners INTEGER,
    lastfm_playcount INTEGER,
    spotify_popularity INTEGER
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

"""


def init_db():
    """Initialize the database and create tables."""
    os.makedirs(os.path.dirname(config.db_path), exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    logger.info("Database initialized at %s", config.db_path)


def _migrate(conn: sqlite3.Connection):
    """Add columns that may not exist in older databases."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()}
    migrations = [
        ("popularity", "INTEGER"),
        ("mb_rating", "REAL"),
        ("mb_rating_count", "INTEGER DEFAULT 0"),
        ("lastfm_listeners", "INTEGER"),
        ("lastfm_playcount", "INTEGER"),
        ("spotify_popularity", "INTEGER"),
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


def get_moods(db: sqlite3.Connection) -> list[dict]:
    """Get all mood tags with counts."""
    rows = db.execute("""
        SELECT mood, COUNT(*) as count
        FROM tracks
        WHERE mood IS NOT NULL AND mood != ''
        GROUP BY mood
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
                   max_per_artist: int = 15) -> list[dict]:
    """
    Query tracks matching AI-generated filters.
    filters can include: genres, year_min, year_max, artists, moods, bpm_min, bpm_max,
                         exclude_genres, exclude_artists, exclude_keywords
    max_per_artist caps how many tracks any single artist can contribute (diversity).
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
        mood_clauses = " OR ".join("LOWER(mood) LIKE ?" for _ in moods)
        conditions.append(f"({mood_clauses})")
        params.extend(f"%{m.lower()}%" for m in moods)

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
               duration, bpm, composer, mood, navidrome_id, file_path,
               popularity
        FROM tracks
        WHERE {where}
        ORDER BY (COALESCE(popularity, 50) + ABS(RANDOM()) % 20) DESC
        LIMIT ?
    """, params).fetchall()

    # Apply per-artist diversity cap
    results = []
    artist_counts: dict[str, int] = {}
    for r in rows:
        d = dict(r)
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
        SELECT id, title, artist, album, genre, year, duration, navidrome_id
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


def get_tracks_missing_spotify(db: sqlite3.Connection, limit: int = 500) -> list[dict]:
    """Get tracks that have been enriched but are missing Spotify data.
    Returns existing Last.fm/MB values so the score can be reblended."""
    rows = db.execute("""
        SELECT id, title, artist, track_number,
               lastfm_listeners, lastfm_playcount,
               mb_rating, mb_rating_count
        FROM tracks
        WHERE popularity IS NOT NULL
          AND spotify_popularity IS NULL
          AND title IS NOT NULL
        ORDER BY id
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_tracks_missing_spotify(db: sqlite3.Connection) -> int:
    """Count enriched tracks that still have no Spotify data."""
    row = db.execute("""
        SELECT COUNT(*) as cnt FROM tracks
        WHERE popularity IS NOT NULL AND spotify_popularity IS NULL AND title IS NOT NULL
    """).fetchone()
    return row["cnt"]


def update_spotify_popularity(db: sqlite3.Connection, rows: list[tuple]):
    """Patch Spotify + reblended popularity for tracks that already have other source data.
    Each row: (popularity, spotify_popularity, track_id)
    """
    db.executemany("""
        UPDATE tracks SET popularity = ?, spotify_popularity = ? WHERE id = ?
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
        UPDATE tracks SET popularity = NULL, mb_rating = NULL,
               mb_rating_count = 0, lastfm_listeners = NULL, lastfm_playcount = NULL
    """)
    count = db.execute("SELECT COUNT(*) as cnt FROM tracks WHERE title IS NOT NULL").fetchone()["cnt"]
    return count


def update_popularity(db: sqlite3.Connection, track_id: int, popularity: int,
                      mb_rating: float | None, mb_rating_count: int,
                      lastfm_listeners: int | None = None,
                      lastfm_playcount: int | None = None,
                      spotify_popularity: int | None = None):
    """Update popularity data for a single track."""
    db.execute("""
        UPDATE tracks
        SET popularity = ?, mb_rating = ?, mb_rating_count = ?,
            lastfm_listeners = ?, lastfm_playcount = ?, spotify_popularity = ?
        WHERE id = ?
    """, (popularity, mb_rating, mb_rating_count,
          lastfm_listeners, lastfm_playcount, spotify_popularity, track_id))


def bulk_update_popularity(db: sqlite3.Connection, rows: list[tuple]):
    """
    Batch-update popularity for multiple tracks in one transaction.
    Each row: (popularity, mb_rating, mb_rating_count, lastfm_listeners,
               lastfm_playcount, spotify_popularity, track_id)
    """
    db.executemany("""
        UPDATE tracks
        SET popularity = ?, mb_rating = ?, mb_rating_count = ?,
            lastfm_listeners = ?, lastfm_playcount = ?, spotify_popularity = ?
        WHERE id = ?
    """, rows)


