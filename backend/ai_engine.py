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

# --- Prompts ---

PASS1_SYSTEM = """You are a music analysis AI. Given a user's playlist prompt and a summary of their music library, extract structured search filters that will help find matching songs.

Respond ONLY with a JSON object (no markdown, no backticks):
{
  "genres": ["genre1", "genre2"],
  "year_min": 1990,
  "year_max": 2005,
  "artists": ["artist1", "artist2"],
  "moods": ["mood1"],
  "bpm_min": null,
  "bpm_max": null,
  "keywords": ["keyword1", "keyword2"],
  "exclude_genres": [],
  "exclude_artists": [],
  "exclude_keywords": []
}

Rules:
- genres: select broadly — include related/adjacent genres. If the prompt is about mood rather than genre, include genres that typically carry that mood.
- artists: only include if the prompt implies specific artists or very specific styles. Leave empty for open-ended prompts.
- years: set range if the prompt implies an era. Use null for no constraint. The year field represents the ORIGINAL release year of the song, not any compilation or re-issue date. So "best of the 90s" means year_min=1990, year_max=1999.
- moods: include if mood tags might exist (e.g. "chill", "energetic", "melancholy", "dark", "uplifting")
- bpm: set range if tempo matters (workout=120-160, chill=60-100, etc). Use null otherwise.
- keywords: additional terms that might appear in song titles, comments, or album names
- exclude_genres: if the prompt says "NOT" or "no" or "without" a genre, put it here (e.g. "jazz but NOT smooth jazz" → exclude_genres: ["smooth jazz"])
- exclude_artists: if the prompt explicitly excludes artists, put them here
- exclude_keywords: if the prompt excludes specific themes or words, put them here
- Cast a WIDE net — it's better to include too many candidates than too few. The second pass will refine.
"""

PASS2_SYSTEM = """You are a music curator. Given a user's playlist prompt and a list of candidate songs from their library, select and order the final playlist.

Candidates are provided in compact format: id;title;artist;album;genre;year;duration;bpm;mood;popularity

Respond ONLY with a JSON object (no markdown, no backticks):
{
  "name": "Playlist Name",
  "description": "Brief 1-2 sentence description of the playlist vibe",
  "song_ids": [123, 456, 789]
}

Rules:
- ONLY select songs from the provided candidate list, using their exact id values
- song_ids is a JSON array of integer IDs in playlist order — no objects, just the IDs
- Order songs for good flow — consider energy arc, key, tempo, and transitions
- Mix artists — don't cluster songs by the same artist unless the prompt asks for it
- You MUST return EXACTLY the number of songs requested. If the user asks for 30 songs, return exactly 30 — not 20, not 25, but exactly 30. Only return fewer if there aren't enough candidates.
- If a target duration is specified, it OVERRIDES the exact song count. Pick songs until the total duration falls within the specified range (±5 minutes of the target). Use the duration field (m:ss format) to track the running total as you select songs.
- If few candidates match, include what fits and explain in the description
- STRONGLY prefer songs with higher popularity scores (pop column, 0-100). These are well-known, beloved tracks that listeners are more likely to enjoy. Avoid obscure deep cuts unless the prompt specifically asks for hidden gems or rare tracks.
- Aim for a playlist that a typical fan of the genre/mood would recognize and enjoy
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
    library_summary should contain: genres, top_artists, year_range
    """
    user_msg = f"""My music library contains:
- Genres: {', '.join(library_summary.get('genres', [])[:80])}
- Top artists ({library_summary.get('artist_count', 0)} total): {', '.join(a['artist'] for a in library_summary.get('top_artists', [])[:50])}
- Years: {library_summary.get('year_range', {}).get('min_year', '?')} to {library_summary.get('year_range', {}).get('max_year', '?')}
- Total songs: {library_summary.get('song_count', 0)}
{f"- Moods tagged: {', '.join(m['mood'] for m in library_summary.get('moods', [])[:40])}" if library_summary.get('moods') else ''}

User's playlist prompt: "{prompt}"

Extract the search filters to find candidate songs."""

    logger.info("Pass 1: extracting intent from prompt (provider: %s)", provider or config.ai_provider)
    text = await _call_ai(PASS1_SYSTEM, user_msg, provider)
    filters = _parse_json(text)
    logger.info("Pass 1 result: %s", json.dumps(filters, indent=2))
    return filters


async def pass2_select_songs(
    prompt: str,
    candidates: list[dict],
    max_songs: int,
    provider: str | None = None,
    target_duration_min: int = None,
) -> dict:
    """
    Pass 2: Select and order songs from the filtered candidates.
    """
    # Build compact candidate list (semicolon-delimited, matches system prompt description)
    candidate_lines = []
    for c in candidates:
        dur = ""
        if c.get("duration") is not None:
            m, s = divmod(int(c["duration"]), 60)
            dur = f"{m}:{s:02d}"
        parts = [
            str(c["id"]),
            c["title"],
            c["artist"],
            c.get("album") or "",
            c.get("genre") or "",
            str(c["year"]) if c.get("year") else "",
            dur,
            str(c["bpm"]) if c.get("bpm") else "",
            c.get("mood") or "",
            str(c["popularity"]) if c.get("popularity") is not None else "",
        ]
        candidate_lines.append(";".join(parts))

    duration_note = ""
    if target_duration_min:
        duration_note = (
            f"\nTarget total playlist duration: {target_duration_min} minutes "
            f"(must be between {target_duration_min - 5} and {target_duration_min + 5} minutes). "
            f"Use the duration column (m:ss) for each candidate to calculate the running total as you pick songs. "
            f"Stop adding songs once the total is within this range."
        )

    user_msg = f"""Playlist prompt: "{prompt}"

Select EXACTLY {max_songs} songs from these {len(candidates)} candidates. You MUST return exactly {max_songs} songs — no fewer, no more.{duration_note}

Candidates:
{chr(10).join(candidate_lines)}"""

    logger.info("Pass 2: selecting from %d candidates (max %d songs)", len(candidates), max_songs)
    text = await _call_ai(PASS2_SYSTEM, user_msg, provider)
    try:
        result = _parse_json(text)
    except (json.JSONDecodeError, ValueError):
        logger.error("Pass 2: failed to parse AI response: %s", text[:500])
        raise ValueError("AI returned an invalid response for Pass 2. Try again.")
    selected = result.get("song_ids") or result.get("songs", [])
    logger.info("Pass 2: AI selected %d songs", len(selected))
    return result
