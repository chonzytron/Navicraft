"""
Two-pass AI playlist generation engine.

Pass 1 — Intent Extraction:
  Send the prompt + library summary (genres, artists, year range) to the AI.
  AI returns structured filters (genres, eras, moods, artists, tempo).

SQLite Query:
  Use filters to query candidates from the local index.

Pass 2 — Song Selection:
  Send prompt + candidate list to AI. AI picks and orders the final playlist.
"""

import asyncio
import json
import logging
import re
import httpx
from config import config

logger = logging.getLogger(__name__)


def _strip_scores(tag_string: str) -> str:
    """Strip confidence scores from scored tag strings for compact AI display.
    'happy:0.85, energetic:0.72' -> 'happy, energetic'
    Also handles legacy format without scores."""
    if not tag_string:
        return ""
    parts = []
    for part in tag_string.split(", "):
        tag_name = part.split(":")[0].strip()
        if tag_name:
            parts.append(tag_name)
    return ", ".join(parts)


# --- Prompts ---

def _build_pass1_system() -> str:
    """Build Pass 1 system prompt with the standardized mood/theme vocabulary."""
    from mood_scanner import MOOD_CATEGORY, THEME_CATEGORY
    vocab = ", ".join(sorted(MOOD_CATEGORY | THEME_CATEGORY))

    return f"""Extract search filters from a playlist prompt. Respond ONLY with JSON (no markdown):
{{"genres":[],"year_min":null,"year_max":null,"artists":[],"moods":[],"bpm_min":null,"bpm_max":null,"keywords":[],"exclude_genres":[],"exclude_artists":[],"exclude_keywords":[],"popularity_mode":false}}

Rules:
- genres: list the specific genres the user asked for, plus closely related sub-genres. STRONGLY PREFER genres from the "Genres:" list provided — those are the actual genres tagged in the library, so matching them gives the best results. You may add a few standard sub-genre names even if not in the list (e.g. "electronic" → also "electro", "techno", "house"), but every genre MUST be a strict sub-genre of what the user asked for. Do NOT add adjacent or tangentially related genres — "electronic" does NOT justify adding "pop", "dance pop", "synth-pop", or "new wave". When in doubt, leave it out.
- artists: only if prompt names specific artists/styles. Empty for open prompts.
- years: null if unconstrained. Refers to ORIGINAL release year, not reissues.
- moods: ONLY from this vocabulary (unrecognized terms match nothing): {vocab}
  IMPORTANT: only set moods when the prompt clearly expresses a mood or emotional context. If the prompt does NOT convey any mood or vibe information, return moods as an empty list. Do NOT infer or guess moods that aren't supported by the prompt.
- bpm: set range if tempo matters (workout/gym=120-160, chill=60-100). null otherwise.
- keywords: terms for song titles, comments, or album names.
- exclude_*: for "NOT"/"no"/"without" exclusions.
- Contextual cues matter: "gym"/"workout"/"running" imply high energy, fast tempo. Reflect this in moods and bpm.
- popularity_mode: set to true when the user asks for "best of", "top hits", "greatest hits", "most popular", or similar popularity-driven requests. When popularity_mode is true:
  - For artist-specific requests (e.g. "best of Daft Punk", "top hits by Queen"): set artists to only that artist, set moods to [], set genres to [], and set bpm to null. The intent is to filter ONLY by artist and rank by popularity.
  - For decade/era requests (e.g. "top hits from the 90s", "best of the 80s"): set the appropriate year_min/year_max (e.g. 1990-1999 for "the 90s"), set moods to [], set genres to [], and set bpm to null. The intent is to filter ONLY by decade and rank by popularity.
  - For other popularity requests: set the relevant filters but always set moods to [] and bpm to null."""

PASS2_SYSTEM = """Select and order songs from candidates for a playlist. Respond ONLY with JSON (no markdown):
{"name":"Playlist Name","description":"Brief vibe description","song_ids":[123,456,789]}

Rules:
- song_ids: integer IDs from candidates only, in playlist order.
- Return EXACTLY the requested count. Only fewer if not enough candidates.
- If target duration given, it overrides count — pick songs until total is within ±5min of target using the dur column (m:ss).
- Order for flow: energy arc, tempo, transitions. Mix artists.
- GENRE FIDELITY IS CRITICAL: every song you pick MUST fit the genres/mood/context described in the prompt. When "Search filters" are provided, cross-check each candidate's genre column against those target genres — a candidate whose genre doesn't overlap with the search filters is almost certainly a bad pick. A popular song that doesn't match the requested genre is a bad pick — skip it regardless of popularity. For example, if the prompt asks for "electronic and hip-hop for the gym", do not include rock, pop, indie, or other off-genre songs even if they are popular.
- SELECTION WEIGHTING: determine from the prompt which dimensions matter most and weight your selection accordingly:
  - Popularity (the pop column) is always an important factor — prefer popular songs over obscure ones when both fit the prompt. It is NOT just a tiebreaker.
  - Genre and artist constraints are hard filters — never violate them.
  - Mood, when present in the search filters, is an additional narrowing filter — strongly prefer songs whose tags match the requested mood, but do not ignore popularity in favour of a slightly better mood match.
  - The prompt itself tells you what to prioritise. "Top hits by X" = popularity-heavy. "Chill jazz for studying" = mood/vibe-heavy. "90s hip hop" = genre+era. Use your judgement.
- DISCOVERY: for genre-based or broad requests (multiple artists in the candidate pool), include a healthy mix of well-known and lesser-known artists (~30% lesser-known) to keep playlists interesting. A great track from a niche artist that fits the vibe perfectly is a good pick. For single-artist requests, this does not apply — just pick that artist's best songs.
- POPULARITY MODE: when search filters include "popularity_mode: true", popularity is the PRIMARY selection criterion. Pick the most popular songs first. For genre or decade requests, still maintain some artist diversity. For single-artist requests, simply rank by popularity. Mood matching does not apply in this mode.
- Match the vibe/energy/context of the prompt (e.g. "gym" = high energy, driving beats).
"""


def _parse_json(text: str) -> dict:
    """Parse JSON from AI response, handling markdown fences and thinking blocks."""
    text = text.strip()
    # Strip markdown code fences (possibly multiple)
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Find the last top-level JSON object — thinking models may emit
        # reasoning text before the actual JSON payload
        last_match = None
        for match in re.finditer(r"\{", text):
            start = match.start()
            try:
                obj = json.loads(text[start:])
                last_match = obj
                break
            except json.JSONDecodeError:
                continue
        # Fallback: grab the largest {...} block
        if last_match is None:
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                return json.loads(match.group())
        if last_match is not None:
            return last_match
        raise ValueError(f"Could not parse AI JSON response: {text[:300]}")


async def _call_claude(system: str, user_message: str) -> str:
    """Call the Claude API and return the text response, with retry for transient errors."""
    max_retries = 3
    for attempt in range(max_retries):
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": config.claude_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": config.claude_model,
                    "max_tokens": 8192,
                    "temperature": 0.7,
                    "system": system,
                    "messages": [{"role": "user", "content": user_message}],
                },
            )

            if resp.status_code in (429, 500, 502, 503, 529) and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("Claude returned %d, retrying in %ds (attempt %d/%d)", resp.status_code, wait, attempt + 1, max_retries)
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 400:
                try:
                    err_msg = resp.json().get("error", {}).get("message", resp.text[:300])
                except Exception:
                    err_msg = resp.text[:300]
                logger.error("Claude API error %d: %s", resp.status_code, err_msg)
                raise ValueError(f"Claude: {err_msg}")
            data = resp.json()

        return "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")

    raise ValueError("Claude API failed after retries")


async def _call_gemini(system: str, user_message: str) -> str:
    """Call the Gemini API and return the text response, with retry for transient errors."""
    max_retries = 3
    for attempt in range(max_retries):
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{config.gemini_model}:generateContent",
                params={"key": config.gemini_api_key},
                headers={"content-type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": system}]},
                    "contents": [{"parts": [{"text": user_message}]}],
                    "generationConfig": {"temperature": 0.7, "maxOutputTokens": 32768},
                },
            )

            if resp.status_code in (429, 500, 502, 503, 529) and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("Gemini returned %d, retrying in %ds (attempt %d/%d)", resp.status_code, wait, attempt + 1, max_retries)
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    err_msg = (body.get("error", {}).get("message")
                               or body.get("message", resp.text[:300]))
                except Exception:
                    err_msg = resp.text[:300]
                logger.error("Gemini API error %d: %s", resp.status_code, err_msg)
                raise ValueError(f"Gemini: {err_msg}")
            data = resp.json()

        text = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                text += part.get("text", "")

        if not text.strip():
            logger.warning("Gemini returned empty response, raw: %s", json.dumps(data)[:500])
            raise ValueError("Gemini returned an empty response")

        return text

    raise ValueError("Gemini API failed after retries")


async def _call_ai(system: str, user_message: str, provider: str | None = None) -> str:
    """Route to configured AI provider. Optional provider overrides config."""
    provider = (provider or config.ai_provider).lower()
    if provider == "claude":
        if not config.claude_api_key:
            raise ValueError("CLAUDE_API_KEY is not set")
        return await _call_claude(system, user_message)
    elif provider == "gemini":
        if not config.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        return await _call_gemini(system, user_message)
    else:
        raise ValueError(f"Unknown AI provider: {provider}")


async def pass1_extract_intent(prompt: str, library_summary: dict, provider: str | None = None) -> dict:
    """
    Pass 1: Extract search filters from the user's prompt.
    library_summary should contain: genres, top_artists, year_range, mood_tags, theme_tags
    """
    system = _build_pass1_system()

    user_msg = f"""Library: {library_summary.get('song_count', 0)} songs, {library_summary.get('artist_count', 0)} artists, years {library_summary.get('year_range', {}).get('min_year', '?')}-{library_summary.get('year_range', {}).get('max_year', '?')}
Genres: {', '.join(library_summary.get('genres', [])[:60])}
Artists: {', '.join(a['artist'] for a in library_summary.get('top_artists', [])[:40])}

Prompt: "{prompt}"
"""

    logger.info("Pass 1: extracting intent from prompt (provider: %s)", provider or config.ai_provider)
    text = await _call_ai(system, user_msg, provider)
    filters = _parse_json(text)
    logger.info("Pass 1 result: %s", json.dumps(filters, indent=2))
    return filters


async def pass2_select_songs(
    prompt: str,
    candidates: list[dict],
    max_songs: int,
    provider: str | None = None,
    target_duration_min: int = None,
    filters: dict | None = None,
) -> dict:
    """
    Pass 2: Select and order songs from the filtered candidates.
    """
    # Build compact candidate list — header row + data rows.
    # Optional columns (bpm, tags, pop) are omitted entirely when < 10%
    # of candidates have data, saving tokens without losing signal.
    threshold = max(1, len(candidates) // 10)
    include_bpm = sum(1 for c in candidates if c.get("bpm")) >= threshold
    include_tags = sum(1 for c in candidates if c.get("mood_tags") or c.get("theme_tags")) >= threshold
    include_pop = sum(1 for c in candidates if c.get("popularity") is not None) >= threshold

    header_parts = ["id", "title", "artist", "genre", "yr", "dur"]
    if include_bpm:
        header_parts.append("bpm")
    if include_tags:
        header_parts.append("tags")
    if include_pop:
        header_parts.append("pop")

    candidate_lines = [";".join(header_parts)]
    for c in candidates:
        dur = ""
        if c.get("duration") is not None:
            m, s = divmod(int(c["duration"]), 60)
            dur = f"{m}:{s:02d}"
        parts = [
            str(c["id"]),
            c["title"],
            c["artist"],
            c.get("genre") or "",
            str(c["year"]) if c.get("year") else "",
            dur,
        ]
        if include_bpm:
            parts.append(str(c["bpm"]) if c.get("bpm") else "")
        if include_tags:
            # Merge mood + theme tags into single field, strip confidence scores
            mood = _strip_scores(c.get("mood_tags") or "")
            theme = _strip_scores(c.get("theme_tags") or "")
            parts.append(",".join(filter(None, [mood, theme])))
        if include_pop:
            parts.append(str(c["popularity"]) if c.get("popularity") is not None else "")
        candidate_lines.append(";".join(parts))

    duration_note = ""
    if target_duration_min:
        duration_note = f"\nTarget duration: {target_duration_min}min (±5min). Use dur column to track total."

    # Include Pass 1 filter context so the AI knows what genres/moods were
    # targeted and can prioritise candidates that genuinely match.
    filter_context = ""
    if filters:
        parts = []
        if filters.get("popularity_mode"):
            parts.append("popularity_mode: true")
        if filters.get("genres"):
            parts.append(f"Genres: {', '.join(filters['genres'])}")
        if filters.get("artists"):
            parts.append(f"Artists: {', '.join(filters['artists'])}")
        if filters.get("moods"):
            parts.append(f"Moods: {', '.join(filters['moods'])}")
        if filters.get("bpm_min") or filters.get("bpm_max"):
            bpm = f"{filters.get('bpm_min', '?')}-{filters.get('bpm_max', '?')}"
            parts.append(f"BPM: {bpm}")
        if parts:
            filter_context = f"\nSearch filters: {'; '.join(parts)}"

    user_msg = f"""Prompt: "{prompt}"
Select {max_songs} songs.{duration_note}{filter_context}

{chr(10).join(candidate_lines)}"""

    cols_included = len(header_parts)
    logger.info("Pass 2: selecting from %d candidates (max %d songs, %d columns)", len(candidates), max_songs, cols_included)
    text = await _call_ai(PASS2_SYSTEM, user_msg, provider)
    try:
        result = _parse_json(text)
    except (json.JSONDecodeError, ValueError):
        logger.error("Pass 2: failed to parse AI response: %s", text[:500])
        raise ValueError("AI returned an invalid response for Pass 2. Try again.")
    selected = result.get("song_ids") or result.get("songs", [])
    logger.info("Pass 2: AI selected %d songs", len(selected))
    return result
