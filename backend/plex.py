"""
Plex integration via Plex HTTP API.
Used for: playlist creation/deletion, and syncing Plex song IDs (ratingKeys) to the local index.
Compatible with Plexamp and all Plex music clients.
"""

import asyncio
import logging
import httpx
from config import config
import database as db

logger = logging.getLogger(__name__)

# Cached after first successful connection
_machine_identifier: str | None = None

_RETRYABLE = (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)


def _headers() -> dict:
    return {
        "X-Plex-Token": config.plex_token,
        "X-Plex-Client-Identifier": "navicraft",
        "X-Plex-Product": "NaviCraft",
        "X-Plex-Version": "2.0.0",
        "Accept": "application/json",
    }


def _api_url(path: str) -> str:
    return f"{config.plex_url.rstrip('/')}{path}"


async def _request(method: str, path: str, params: dict = None) -> dict | None:
    """HTTP request with exponential backoff retries."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.request(method, _api_url(path), headers=_headers(), params=params or {})
                resp.raise_for_status()
                if method == "DELETE":
                    return None
                ct = resp.headers.get("content-type", "")
                if ct.startswith("application/json"):
                    return resp.json()
                return {}
        except _RETRYABLE as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("Plex %s %s failed (%s), retrying in %ds", method, path, type(e).__name__, wait)
                await asyncio.sleep(wait)
            else:
                raise ConnectionError(f"Cannot reach Plex at {config.plex_url}: {type(e).__name__}")


async def _get(path: str, params: dict = None) -> dict:
    return await _request("GET", path, params)


async def _post(path: str, params: dict = None) -> dict:
    return await _request("POST", path, params)


async def _delete(path: str) -> None:
    await _request("DELETE", path)


async def _get_machine_identifier() -> str:
    """Fetch and cache the Plex server's machineIdentifier (needed for playlist URIs)."""
    global _machine_identifier
    if _machine_identifier:
        return _machine_identifier

    data = await _get("/")
    mi = data.get("MediaContainer", {}).get("machineIdentifier")
    if not mi:
        raise Exception("Could not determine Plex machineIdentifier")
    _machine_identifier = mi
    return mi


def _make_uri(machine_id: str, rating_keys: list[str]) -> str:
    """Build the server:// URI that Plex uses for playlist items."""
    keys = ",".join(rating_keys)
    return f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{keys}"


async def _get_music_section_id() -> str:
    """Find the first music library section."""
    data = await _get("/library/sections")
    sections = data.get("MediaContainer", {}).get("Directory", [])
    for s in sections:
        if s.get("type") == "artist":
            return str(s["key"])
    raise Exception("No music library section found in Plex. Ensure you have a music library configured.")


async def test_connection() -> dict:
    """Test connectivity to Plex and return server info."""
    data = await _get("/")
    mc = data.get("MediaContainer", {})
    return {
        "status": "ok",
        "version": mc.get("version", "unknown"),
        "friendlyName": mc.get("friendlyName", "Plex"),
    }


async def sync_plex_ids():
    """
    Fetch all tracks from Plex and match them to local index entries
    by comparing file paths or by artist+title fallback.
    """
    logger.info("Syncing Plex song IDs...")

    section_id = await _get_music_section_id()

    # Fetch all tracks from the music section in batches
    all_plex_tracks = []
    offset = 0
    batch = 500
    while True:
        data = await _get(f"/library/sections/{section_id}/all", {
            "type": 10,  # type 10 = track
            "X-Plex-Container-Start": offset,
            "X-Plex-Container-Size": batch,
        })
        mc = data.get("MediaContainer", {})
        tracks = mc.get("Metadata", [])
        if not tracks:
            break
        all_plex_tracks.extend(tracks)
        if len(tracks) < batch:
            break
        offset += batch

    logger.info("Fetched %d tracks from Plex", len(all_plex_tracks))

    if not all_plex_tracks:
        return 0

    # Build lookup maps: path -> plex ratingKey, (artist, title) -> ratingKey
    path_map = {}
    title_map = {}
    music_dir = config.music_dir.rstrip("/")
    tracks_with_paths = 0

    for t in all_plex_tracks:
        rating_key = str(t.get("ratingKey", ""))
        plex_artist = (t.get("grandparentTitle") or t.get("originalTitle") or "").lower().strip()
        plex_title = (t.get("title") or "").lower().strip()

        # Extract file path from Media -> Part -> file
        media_list = t.get("Media", [])
        for media in media_list:
            parts = media.get("Part", [])
            for part in parts:
                plex_path = part.get("file", "")
                if plex_path:
                    # Plex stores absolute paths
                    path_map[plex_path] = rating_key
                    # Also try relative (strip music_dir prefix)
                    if plex_path.startswith(music_dir):
                        rel = plex_path[len(music_dir):].lstrip("/")
                        path_map[rel] = rating_key
                    tracks_with_paths += 1

        if plex_artist and plex_title:
            title_map[(plex_artist, plex_title)] = rating_key

    logger.info("Built path map with %d paths, %d artist+title entries", tracks_with_paths, len(title_map))

    # Match against local index
    matched = 0
    with db.get_db() as conn:
        rows = conn.execute("SELECT file_path, artist, title FROM tracks").fetchall()
        mapping = {}

        for row in rows:
            fp = row["file_path"]
            plex_id = None

            # Try absolute path match
            plex_id = path_map.get(fp)

            if not plex_id:
                # Try relative path (strip music_dir prefix)
                if fp.startswith(music_dir):
                    rel = fp[len(music_dir):].lstrip("/")
                    plex_id = path_map.get(rel)

            if not plex_id:
                # Fallback: artist + title
                artist = (row["artist"] or "").lower().strip()
                title = (row["title"] or "").lower().strip()
                if artist and title:
                    plex_id = title_map.get((artist, title))

            if plex_id:
                mapping[fp] = plex_id
                matched += 1

        if mapping:
            db.bulk_update_plex_ids(conn, mapping)

    logger.info("Synced %d / %d Plex IDs", matched, len(rows))
    return matched


async def create_playlist(name: str, song_ids: list[str]) -> dict:
    """Create a playlist in Plex. song_ids are Plex ratingKeys."""
    machine_id = await _get_machine_identifier()
    uri = _make_uri(machine_id, song_ids)

    data = await _post("/playlists", {
        "type": "audio",
        "title": name,
        "smart": 0,
        "uri": uri,
    })

    mc = data.get("MediaContainer", {})
    playlists = mc.get("Metadata", [])
    if not playlists:
        raise Exception("Plex did not return playlist data. The playlist may not have been created.")

    pl = playlists[0]
    return {
        "id": str(pl.get("ratingKey", "")),
        "name": pl.get("title", name),
        "songCount": pl.get("leafCount", len(song_ids)),
    }


async def get_playlists() -> list[dict]:
    """List audio playlists from Plex."""
    data = await _get("/playlists", {"playlistType": "audio"})
    playlists = data.get("MediaContainer", {}).get("Metadata", [])
    return [
        {
            "id": str(p.get("ratingKey", "")),
            "name": p.get("title", ""),
            "songCount": p.get("leafCount", 0),
            "duration": p.get("duration", 0) // 1000,  # Plex returns ms
        }
        for p in playlists
    ]


async def delete_playlist(playlist_id: str):
    """Delete a playlist from Plex."""
    await _delete(f"/playlists/{playlist_id}")
