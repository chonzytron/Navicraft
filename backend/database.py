"""
SQLite database for the local music index.
Stores rich metadata per track, supports filtering queries for the AI pipeline.
"""

import sqlite3
import os
import random
import re
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
CREATE INDEX IF NOT EXISTS idx_popularity ON tracks(popularity);
CREATE INDEX IF NOT EXISTS idx_artist_popularity ON tracks(artist, popularity);

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
        # Partial index speeds up the mood scanner's hot query
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_essentia_pending "
            "ON tracks(id) WHERE essentia_scanned_at IS NULL"
        )
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


def _sqlite_regexp(pattern: str, value: Optional[str]) -> int:
    """SQLite REGEXP implementation: `value REGEXP pattern` -> regexp(pattern, value)."""
    if value is None:
        return 0
    return 1 if re.search(pattern, value) else 0


def _word_pattern(term: str) -> str:
    """Build a case-insensitive, token-bounded regex for an artist/genre name.

    Uses lookarounds rather than ``\\b`` so names that begin/end with non-word
    characters (e.g. "!!!") still match, while ensuring the term isn't flanked by
    alphanumerics. This stops "Queen" from matching "Queens of the Stone Age" while
    still matching "Queen Latifah" or "The Beatles".
    """
    return r"(?i)(?<![a-z0-9])" + re.escape(term.strip()) + r"(?![a-z0-9])"


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(config.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_function("regexp", 2, _sqlite_regexp, deterministic=True)
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


def bulk_update_navidrome_ids(db: sqlite3.Connection, mapping: dict):
    """Bulk update navidrome IDs. mapping = {file_path: navidrome_id}"""
    db.executemany(
        "UPDATE tracks SET navidrome_id = ? WHERE file_path = ?",
        [(nid, fp) for fp, nid in mapping.items()]
    )


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


# Genre field separators. Tracks can carry several genres; we split on ';', '/'
# and null bytes — but NOT commas, since some single genres contain them
# (e.g. Discogs's "Folk, World, & Country" or "Stage & Screen").
_GENRE_SEP = re.compile(r"[;/\x00]+")


def split_genres(value: str | None) -> list[str]:
    """Split a stored/raw genre field into individual genre names (whitespace
    stripped, empties dropped). The single source of truth for genre tokenizing,
    used by both the scanner (at index time) and the genre summary."""
    if not value:
        return []
    return [g.strip() for g in _GENRE_SEP.split(value) if g.strip()]


def get_genres(db: sqlite3.Connection) -> list[dict]:
    """Get all genres with song counts.

    A track may be tagged with multiple genres (stored joined in the `genre`
    column), so we split and count each individually in Python — a track tagged
    "Rock; Alternative" contributes to both. Mirrors get_mood_tag_summary."""
    rows = db.execute(
        "SELECT genre FROM tracks WHERE genre IS NOT NULL AND genre != ''"
    ).fetchall()
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for r in rows:
        for g in split_genres(r["genre"]):
            key = g.lower()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, g)
    return [
        {"genre": display[k], "count": c}
        for k, c in sorted(counts.items(), key=lambda x: -x[1])
    ]


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


# Popularity bucket thresholds for variety sampling.
# Tracks split into 3 tiers by popularity score (0-100 scale from enrichment).
# When no specific artist is requested, candidates are drawn proportionally
# from each tier so the AI sees a mix of popular, mid-tier and niche tracks
# rather than a top-heavy list.
_BUCKET_TOP = 65
_BUCKET_MID = 25
_BUCKET_SHARES = (
    ("top", 0.35, f"COALESCE(popularity, 0) >= {_BUCKET_TOP}"),
    ("mid", 0.45, f"COALESCE(popularity, 0) >= {_BUCKET_MID} AND COALESCE(popularity, 0) < {_BUCKET_TOP}"),
    ("niche", 0.20, f"popularity IS NULL OR popularity < {_BUCKET_MID}"),
)


def _build_filter_where(filters: dict) -> tuple[str, list]:
    """Build the SQL WHERE clause and params list for a filter dict.
    Shared by filter_tracks and its bucket variants."""
    conditions = ["title IS NOT NULL"]
    params: list = []

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
        # Token-bounded regex (not substring): "Queen" must not match
        # "Queens of the Stone Age". Exact matches are ranked first in filter_tracks.
        # A cheap LIKE pre-check gates the per-row Python regexp so it only runs on
        # rows that actually contain the term (substring is a necessary condition
        # for any word-boundary match) — keeps full-table scans fast.
        clauses = []
        for a in artists:
            like = f"%{a.strip().lower()}%"
            word = _word_pattern(a)
            clauses.append(
                "((LOWER(artist) LIKE ? AND artist REGEXP ?) "
                "OR (LOWER(album_artist) LIKE ? AND album_artist REGEXP ?))"
            )
            params.extend([like, word, like, word])
        conditions.append("(" + " OR ".join(clauses) + ")")

    if filters.get("moods"):
        # Token-bounded like artists: the vocabulary contains proper-substring
        # pairs ("fun"/"funny", "drama"/"dramatic"), so a plain LIKE would let
        # wrong-mood tracks into the pool and suppress relaxation. Tags are
        # stored as "name:score, ..." so the word boundary is clean. Same cheap
        # LIKE gate as the artist filter keeps the Python regexp off most rows.
        clauses = []
        for m in filters["moods"]:
            like = f"%{m.strip().lower()}%"
            word = _word_pattern(m)
            clauses.append(
                "((LOWER(mood_tags) LIKE ? AND mood_tags REGEXP ?) "
                "OR (LOWER(theme_tags) LIKE ? AND theme_tags REGEXP ?))"
            )
            params.extend([like, word, like, word])
        conditions.append("(" + " OR ".join(clauses) + ")")

    if filters.get("bpm_min"):
        conditions.append("bpm >= ?")
        params.append(filters["bpm_min"])

    if filters.get("bpm_max"):
        conditions.append("bpm <= ?")
        params.append(filters["bpm_max"])

    if filters.get("keywords"):
        keywords = filters["keywords"]
        kw_clauses = " OR ".join(
            "(LOWER(title) LIKE ? OR LOWER(album) LIKE ? OR LOWER(comment) LIKE ?)"
            for _ in keywords
        )
        conditions.append(f"({kw_clauses})")
        for kw in keywords:
            params.extend([f"%{kw.lower()}%", f"%{kw.lower()}%", f"%{kw.lower()}%"])

    if filters.get("exclude_genres"):
        # Token-bounded so excluding "rap" doesn't also drop "trap"; NULL-safe so
        # untagged tracks aren't silently removed by an exclusion (NULL NOT LIKE x
        # would otherwise evaluate falsey and exclude them). The NOT LIKE short-
        # circuits the regexp for rows that don't contain the term at all.
        for eg in filters["exclude_genres"]:
            conditions.append("(genre IS NULL OR LOWER(genre) NOT LIKE ? OR NOT (genre REGEXP ?))")
            params.extend([f"%{eg.strip().lower()}%", _word_pattern(eg)])

    if filters.get("exclude_artists"):
        for ea in filters["exclude_artists"]:
            conditions.append("(artist IS NULL OR LOWER(artist) NOT LIKE ? OR NOT (artist REGEXP ?))")
            params.extend([f"%{ea.strip().lower()}%", _word_pattern(ea)])

    if filters.get("exclude_keywords"):
        # NULL-safe on album (title is guaranteed by the base condition):
        # NULL NOT LIKE x evaluates to NULL, which would silently exclude
        # every album-less track from any keyword-exclusion query.
        for ek in filters["exclude_keywords"]:
            conditions.append("(LOWER(title) NOT LIKE ? AND (album IS NULL OR LOWER(album) NOT LIKE ?))")
            params.extend([f"%{ek.lower()}%", f"%{ek.lower()}%"])

    return " AND ".join(conditions), params


_TRACK_COLS = (
    "id, title, artist, album_artist, album, genre, year, "
    "duration, bpm, composer, mood, navidrome_id, plex_id, file_path, "
    "popularity, mood_tags, theme_tags"
)


def _mood_match_score(track: dict, mood_set: set[str]) -> float:
    """Sum of confidence scores for tags that match the requested moods.
    Matches both mood_tags and theme_tags, since the AI's 'moods' filter
    draws from the combined vocabulary."""
    if not mood_set:
        return 0.0
    total = 0.0
    for col in ("mood_tags", "theme_tags"):
        for name, score in _parse_scored_tags_with_scores(track.get(col) or ""):
            if name.lower() in mood_set:
                total += score
    return total


def _artist_exact_order(filters: dict) -> tuple[str, list]:
    """Build an ORDER BY prefix that ranks exact artist matches first.

    Token-bounded WHERE matching still lets "Queen" match "Queen Latifah"; this
    expression floats the genuine "Queen" tracks above near-name collisions so
    artist-specific / popularity-mode requests stay on-target. Returns ("", [])
    when no artists are requested.
    """
    artists = [a.lower().strip() for a in (filters.get("artists") or []) if a and a.strip()]
    if not artists:
        return "", []
    placeholders = ",".join("?" for _ in artists)
    expr = (
        f"(CASE WHEN LOWER(TRIM(artist)) IN ({placeholders}) "
        f"OR LOWER(TRIM(album_artist)) IN ({placeholders}) THEN 1 ELSE 0 END) DESC, "
    )
    return expr, artists + artists


# Jitter expression for variety ordering. Uses (RANDOM() % N + N) % N rather than
# ABS(RANDOM()) — ABS() raises an integer-overflow error for the single value
# RANDOM() == -2^63, which would fail the whole candidate query.
def _jitter(n: int) -> str:
    return f"(RANDOM() % {n} + {n}) % {n}"


def _bucket_of(popularity: int | None) -> str:
    """Classify a popularity score into its bucket name."""
    if popularity is None:
        return "niche"
    if popularity >= _BUCKET_TOP:
        return "top"
    if popularity >= _BUCKET_MID:
        return "mid"
    return "niche"


def filter_tracks(db: sqlite3.Connection, filters: dict, limit: int = 500,
                   max_songs: int | None = None, popularity_order: bool = False) -> list[dict]:
    """Query tracks matching AI-generated filters.

    filters can include: genres, year_min, year_max, artists, moods, bpm_min, bpm_max,
                         exclude_genres, exclude_artists, exclude_keywords.

    Candidate selection strategy:
      - popularity_order=True: strict popularity-desc ordering ("best of" / "top hits").
      - Artist filter present: popularity-weighted single pass (the user wants that
        artist's output; no need to force variety).
      - Otherwise (broad prompt): proportional bucket sampling across top/mid/niche
        popularity tiers so the AI sees a diverse candidate pool rather than only
        the most popular tracks. Each bucket is capped at its target share of
        `limit` during final selection, then any leftover quota (e.g. a bucket
        with fewer tracks than its share) is filled from the remaining pool.

    When a mood filter is present, the merged candidate set is re-ranked by summed
    mood/theme confidence score (popularity as the tiebreaker) so the strongest mood
    matches surface first while the bucket mix preserves popularity variety. Buckets
    also fetch a larger slice in mood mode so high-confidence-but-obscure tracks
    aren't truncated away before the re-rank.

    Per-artist diversity cap: 30% of max_songs (min 3) when no specific artists are
    requested. Skipped when artists are specified.
    """
    where, params = _build_filter_where(filters)

    mood_set = {m.lower() for m in (filters.get("moods") or [])}
    has_artist_filter = bool(filters.get("artists"))
    use_buckets = not popularity_order and not has_artist_filter
    exact_expr, exact_params = _artist_exact_order(filters)

    rows: list[sqlite3.Row] = []
    if popularity_order:
        sql_params = list(params) + exact_params + [limit * 3]
        rows = db.execute(
            f"SELECT {_TRACK_COLS} FROM tracks WHERE {where} "
            f"ORDER BY {exact_expr}COALESCE(popularity, 0) DESC LIMIT ?",
            sql_params,
        ).fetchall()
    elif use_buckets:
        # Fetch from each popularity bucket with headroom for dedupe +
        # per-artist cap. The final selection loop below enforces proportional
        # representation using bucket_quotas — without that step, the first
        # bucket's rows would fill the `limit` before mid/niche get a chance.
        # When a mood filter is active, widen the fetch so the Python mood re-rank
        # has more to work with (mood confidence can't be ordered in SQL).
        fetch_mult = 5 if mood_set else 3
        for _name, share, bucket_clause in _BUCKET_SHARES:
            bucket_limit = max(20, round(limit * share * fetch_mult))
            bucket_sql = (
                f"SELECT {_TRACK_COLS} FROM tracks WHERE {where} AND ({bucket_clause}) "
                f"ORDER BY (COALESCE(popularity, 10) * 0.6 + {_jitter(40)}) DESC "
                f"LIMIT ?"
            )
            rows.extend(db.execute(bucket_sql, [*params, bucket_limit]).fetchall())
    else:
        # Artist-specific prompt: single popularity-weighted pass.
        sql_params = list(params) + exact_params + [limit * 3]
        rows = db.execute(
            f"SELECT {_TRACK_COLS} FROM tracks WHERE {where} "
            f"ORDER BY {exact_expr}(COALESCE(popularity, 30) * 0.7 + {_jitter(30)}) DESC "
            f"LIMIT ?",
            sql_params,
        ).fetchall()

    # Parse rows and compute mood confidence scores.
    parsed = [dict(r) for r in rows]
    if mood_set:
        for t in parsed:
            t["_mood_score"] = _mood_match_score(t, mood_set)
        # Sort by mood score primarily, popularity secondary — keeps the
        # strongest mood matches at the top while still surfacing popular
        # tracks when mood confidence ties.
        parsed.sort(
            key=lambda t: (
                -(t.get("_mood_score") or 0.0),
                -(t.get("popularity") or 0),
            )
        )

    # De-dupe (bucket queries can overlap at boundaries if popularity changes mid-query).
    seen_ids: set[int] = set()
    deduped: list[dict] = []
    for t in parsed:
        tid = t.get("id")
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        deduped.append(t)

    # Per-artist diversity cap — skipped when specific artists requested.
    if has_artist_filter or max_songs is None:
        max_per_artist = None
    else:
        max_per_artist = max(3, round(max_songs * 0.3))

    # Per-bucket quotas enforce proportional popularity variety in the output.
    # Only applied when use_buckets is True; for popularity_order / artist-specific
    # searches the caller wants a single ordered list, not a mix.
    if use_buckets:
        bucket_quotas = {name: max(1, round(limit * share)) for name, share, _ in _BUCKET_SHARES}
    else:
        bucket_quotas = None
    bucket_counts: dict[str, int] = {name: 0 for name in (bucket_quotas or {})}

    results: list[dict] = []
    artist_counts: dict[str, int] = {}
    deferred: list[dict] = []  # tracks skipped due to a full bucket quota

    for d in deduped:
        # Per-artist cap
        if max_per_artist is not None:
            artist_key = (d.get("artist") or "").lower().strip()
            if artist_counts.get(artist_key, 0) >= max_per_artist:
                continue

        # Per-bucket quota (variety mode only)
        if bucket_quotas is not None:
            bname = _bucket_of(d.get("popularity"))
            if bucket_counts[bname] >= bucket_quotas[bname]:
                deferred.append(d)
                continue
            bucket_counts[bname] += 1

        if max_per_artist is not None:
            artist_key = (d.get("artist") or "").lower().strip()
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        results.append(d)
        if len(results) >= limit:
            break

    # Second pass: fill remaining slots from deferred rows (buckets that had more
    # candidates than their quota). Handles cases where one bucket is undersized
    # (e.g. small library with few niche tracks) — we still want to hit `limit`.
    if len(results) < limit and deferred:
        for d in deferred:
            if max_per_artist is not None:
                artist_key = (d.get("artist") or "").lower().strip()
                if artist_counts.get(artist_key, 0) >= max_per_artist:
                    continue
                artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
            results.append(d)
            if len(results) >= limit:
                break

    # In variety mode the results are concatenated top→mid→niche tier blocks.
    # Interleave them so Pass 2 doesn't positionally anchor on the popular tier
    # and can build the intended popular/mid/niche mix. Mood mode keeps its
    # confidence ordering; popularity/artist modes keep their deliberate order.
    if use_buckets and not mood_set:
        random.shuffle(results)

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


# --- Per-source enrichment helpers (Deezer / Last.fm / MusicBrainz) ---
# These three sources share identical query/update shapes, differing only by
# column. Parameterizing by `source` keeps them in lockstep. `source` is always
# an internal literal (never user input), so interpolating column names is safe.

_SOURCE_DATA_COL = {
    "deezer": "deezer_rank",
    "lastfm": "lastfm_listeners",
    "musicbrainz": "musicbrainz_rating",
}
_SOURCE_CHECKED_COL = {
    "deezer": "deezer_checked_at",
    "lastfm": "lastfm_checked_at",
    "musicbrainz": "musicbrainz_checked_at",
}
# Columns SET by each source's reblend writer. The row tuple is always
# (popularity, <these columns...>, track_id).
_SOURCE_UPDATE_COLS = {
    "deezer": ("deezer_rank", "deezer_id", "deezer_checked_at"),
    "lastfm": ("lastfm_listeners", "lastfm_playcount", "lastfm_checked_at"),
    "musicbrainz": ("musicbrainz_rating", "musicbrainz_rating_count", "musicbrainz_checked_at"),
}


def get_tracks_missing_source(db: sqlite3.Connection, source: str, limit: int = 500) -> list[dict]:
    """Get enriched tracks missing `source` data and due for a retry.
    Tracks checked in the last 24h with no result are skipped (retry tomorrow).
    Returns all sibling source columns so the popularity score can be reblended."""
    data_col = _SOURCE_DATA_COL[source]
    checked_col = _SOURCE_CHECKED_COL[source]
    rows = db.execute(f"""
        SELECT id, title, artist, track_number,
               deezer_rank, lastfm_listeners, lastfm_playcount,
               musicbrainz_rating, musicbrainz_rating_count
        FROM tracks
        WHERE popularity IS NOT NULL
          AND {data_col} IS NULL
          AND title IS NOT NULL
          AND ({checked_col} IS NULL OR (unixepoch() - {checked_col}) > 86400)
        ORDER BY {checked_col} ASC NULLS FIRST, id ASC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_tracks_missing_source(db: sqlite3.Connection, source: str) -> int:
    """Count enriched tracks that still have no `source` data and are due for a retry."""
    data_col = _SOURCE_DATA_COL[source]
    checked_col = _SOURCE_CHECKED_COL[source]
    row = db.execute(f"""
        SELECT COUNT(*) as cnt FROM tracks
        WHERE popularity IS NOT NULL AND {data_col} IS NULL AND title IS NOT NULL
          AND ({checked_col} IS NULL OR (unixepoch() - {checked_col}) > 86400)
    """).fetchone()
    return row["cnt"]


def update_source_not_found(db: sqlite3.Connection, source: str, track_ids: list[int]):
    """Mark tracks as checked for `source` but not found. They won't be retried for 24h."""
    if not track_ids:
        return
    checked_col = _SOURCE_CHECKED_COL[source]
    now = time.time()
    db.executemany(
        f"UPDATE tracks SET {checked_col} = ? WHERE id = ?",
        [(now, tid) for tid in track_ids],
    )


def update_source_popularity(db: sqlite3.Connection, source: str, rows: list[tuple]):
    """Patch `source` columns + reblended popularity for tracks that already have
    other source data. Each row: (popularity, <source columns...>, track_id)."""
    set_cols = ", ".join(f"{c} = ?" for c in ("popularity", *_SOURCE_UPDATE_COLS[source]))
    db.executemany(f"UPDATE tracks SET {set_cols} WHERE id = ?", rows)


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


def tag_names(tag_string: str) -> list[str]:
    """Parse a scored tag string like 'happy:0.85, energetic:0.72' into tag names.
    Also handles legacy format without scores (e.g. 'happy, energetic').
    Splits on ',' and strips whitespace so missing-space variants aren't dropped.
    Shared by the library summaries and the AI engine's compact tag rendering."""
    if not tag_string:
        return []
    tags = []
    for part in tag_string.split(","):
        part = part.strip()
        if not part:
            continue
        tag_name = part.split(":")[0].strip()
        if tag_name:
            tags.append(tag_name)
    return tags


def _parse_scored_tags_with_scores(tag_string: str) -> list[tuple[str, float]]:
    """Parse a scored tag string into (tag, score) pairs. Legacy unscored tags get 1.0."""
    if not tag_string:
        return []
    result = []
    for part in tag_string.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, _, score_str = part.partition(":")
            name = name.strip()
            try:
                score = float(score_str.strip())
            except (ValueError, TypeError):
                score = 1.0
        else:
            name, score = part, 1.0
        if name:
            result.append((name, score))
    return result


def _tag_summary(db: sqlite3.Connection, column: str) -> list[dict]:
    """Get distinct tags with approximate counts for a mood_tags/theme_tags column.
    Parses comma-separated values (with optional confidence scores) in Python.
    `column` is an internal literal, never user input."""
    rows = db.execute(
        f"SELECT {column} FROM tracks WHERE {column} IS NOT NULL AND {column} != ''"
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for tag in tag_names(r[column]):
            counts[tag] = counts.get(tag, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: -x[1])]


def get_mood_tag_summary(db: sqlite3.Connection) -> list[dict]:
    """Get distinct mood tags with approximate counts."""
    return _tag_summary(db, "mood_tags")


def get_theme_tag_summary(db: sqlite3.Connection) -> list[dict]:
    """Get distinct theme tags with approximate counts."""
    return _tag_summary(db, "theme_tags")


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


