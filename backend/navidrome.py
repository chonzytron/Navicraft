"""
Navidrome integration via Subsonic API.
Used for: playlist creation/deletion, and syncing Navidrome song IDs to the local index.
"""

import asyncio
import hashlib
import secrets
import logging
import httpx
from config import config
import database as db

logger = logging.getLogger(__name__)


def _subsonic_params() -> dict:
    salt = secrets.token_hex(8)
    token = hashlib.md5((config.navidrome_password + salt).encode()).hexdigest()
    return {
        "u": config.navidrome_user,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "navicraft",
        "f": "json",
    }


def _api_url(endpoint: str) -> str:
    return f"{config.navidrome_url.rstrip('/')}/rest/{endpoint}"


async def _get(endpoint: str, params: dict = None) -> dict:
    all_params = _subsonic_params()
    if params:
        all_params.update(params)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(_api_url(endpoint), params=all_params)
                resp.raise_for_status()
                data = resp.json()
            break
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("Navidrome request failed (%s: %s), retrying in %ds", type(e).__name__, e, wait)
                await asyncio.sleep(wait)
            else:
                raise ConnectionError(f"Cannot reach Navidrome at {config.navidrome_url}: {type(e).__name__}")
    sr = data.get("subsonic-response", {})
    if sr.get("status") != "ok":
        error = sr.get("error", {})
        raise Exception(f"Subsonic error: {error.get('message', 'Unknown')}")
    return sr


async def test_connection() -> dict:
    sr = await _get("ping")
    return {"status": "ok", "version": sr.get("version", "unknown")}


async def sync_navidrome_ids():
    """
    Fetch all songs from Navidrome and match them to local index entries
    by comparing file paths (stripped to relative) or by artist+title.
    """
    logger.info("Syncing Navidrome song IDs...")

    # Fetch all songs from Navidrome in batches
    all_nd_songs = []
    offset = 0
    batch = 500
    while True:
        data = await _get("search3", {
            "query": "",
            "songCount": batch,
            "songOffset": offset,
            "albumCount": 0,
            "artistCount": 0,
        })
        songs = data.get("searchResult3", {}).get("song", [])
        if not songs:
            break
        all_nd_songs.extend(songs)
        if len(songs) < batch:
            break
        offset += batch

    logger.info("Fetched %d songs from Navidrome", len(all_nd_songs))

    if not all_nd_songs:
        return 0

    # Build lookup: normalize path -> navidrome id
    # Also build artist+title -> id as fallback
    path_map = {}
    title_map = {}
    music_dir = config.music_dir.rstrip("/")

    for s in all_nd_songs:
        nd_id = s.get("id", "")
        nd_path = s.get("path", "")
        nd_artist = (s.get("artist") or "").lower().strip()
        nd_title = (s.get("title") or "").lower().strip()

        if nd_path:
            # Navidrome stores relative paths from its music folder
            # Try matching both absolute and relative
            path_map[nd_path] = nd_id
            abs_path = f"{music_dir}/{nd_path}"
            path_map[abs_path] = nd_id

        if nd_artist and nd_title:
            title_map[(nd_artist, nd_title)] = nd_id

    # Match against local index
    matched = 0
    with db.get_db() as conn:
        rows = conn.execute("SELECT file_path, artist, title FROM tracks").fetchall()
        mapping = {}

        for row in rows:
            fp = row["file_path"]
            nd_id = None

            # Try path match
            nd_id = path_map.get(fp)

            if not nd_id:
                # Try relative path (strip music_dir prefix)
                if fp.startswith(music_dir):
                    rel = fp[len(music_dir):].lstrip("/")
                    nd_id = path_map.get(rel)

            if not nd_id:
                # Fallback: artist + title
                artist = (row["artist"] or "").lower().strip()
                title = (row["title"] or "").lower().strip()
                if artist and title:
                    nd_id = title_map.get((artist, title))

            if nd_id:
                mapping[fp] = nd_id
                matched += 1

        if mapping:
            db.bulk_update_navidrome_ids(conn, mapping)

    logger.info("Synced %d / %d Navidrome IDs", matched, len(rows))
    return matched


async def create_playlist(name: str, song_ids: list[str]) -> dict:
    """Create a playlist in Navidrome. song_ids are Navidrome IDs."""
    salt = secrets.token_hex(8)
    token = hashlib.md5((config.navidrome_password + salt).encode()).hexdigest()

    query_params = [
        ("u", config.navidrome_user),
        ("t", token),
        ("s", salt),
        ("v", "1.16.1"),
        ("c", "navicraft"),
        ("f", "json"),
        ("name", name),
    ]
    for sid in song_ids:
        query_params.append(("songId", sid))

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(_api_url("createPlaylist"), params=query_params)
                resp.raise_for_status()
                data = resp.json()
            break
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                logger.warning("Playlist create failed (%s: %s), retrying in %ds", type(e).__name__, e, wait)
                await asyncio.sleep(wait)
            else:
                raise ConnectionError(f"Cannot reach Navidrome at {config.navidrome_url}: {type(e).__name__}")

    sr = data.get("subsonic-response", {})
    if sr.get("status") != "ok":
        error = sr.get("error", {})
        raise Exception(f"Failed to create playlist: {error.get('message')}")

    playlist = sr.get("playlist", {})
    return {
        "id": playlist.get("id", ""),
        "name": playlist.get("name", name),
        "songCount": playlist.get("songCount", len(song_ids)),
    }


async def get_playlists() -> list[dict]:
    data = await _get("getPlaylists")
    playlists = data.get("playlists", {}).get("playlist", [])
    return [
        {
            "id": p["id"],
            "name": p.get("name", ""),
            "songCount": p.get("songCount", 0),
            "duration": p.get("duration", 0),
        }
        for p in playlists
    ]


async def delete_playlist(playlist_id: str):
    await _get("deletePlaylist", {"id": playlist_id})
