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

PASS1_SYSTEM = None  # Built dynamically with mood vocabulary in pass1_extract_intent()


def _build_pass1_system() -> str:
    """Build Pass 1 system prompt with the standardized mood/theme vocabulary."""
    from mood_scanner import MOOD_CATEGORY, THEME_CATEGORY
    vocab = ", ".join(sorted(MOOD_CATEGORY | THEME_CATEGORY))

    return f"""Extract search filters from a playlist prompt. Respond ONLY with JSON (no markdown):
{{"genres":[],"year_min":null,"year_max":null,"artists":[],"moods":[],"bpm_min":null,"bpm_max":null,"keywords":[],"exclude_genres":[],"exclude_artists":[],"exclude_keywords":[]}}

Rules:
- genres: broad — include related/adjacent genres. For mood-based prompts, include genres that carry that mood.
- artists: only if prompt names specific artists/styles. Empty for open prompts.
- years: null if unconstrained. Refers to ORIGINAL release year, not reissues.
- moods: ONLY from this vocabulary (unrecognized terms match nothing): {vocab}
- bpm: set range if tempo matters (workout=120-160, chill=60-100). null otherwise.
- keywords: terms for song titles, comments, or album names.
- exclude_*: for "NOT"/"no"/"without" exclusions.
- Cast a wide net — better too many candidates than too few."""

PASS2_SYSTEM = """Select and order songs from candidates for a playlist. Respond ONLY with JSON (no markdown):
{"name":"Playlist Name","description":"Brief vibe description","song_ids":[123,456,789]}

Rules:
- song_ids: integer IDs from candidates only, in playlist order.
- Return EXACTLY the requested count. Only fewer if not enough candidates.
- If target duration given, it overrides count — pick songs until total is within ±5min of target using the dur column (m:ss).
- Order for flow: energy arc, tempo, transitions. Mix artists.
- Prefer higher popularity (pop 0-100). Avoid deep cuts unless prompt asks for them.
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
) -> dict:
    """
    Pass 2: Select and order songs from the filtered candidates.
    """
    # Build compact candidate list — header row + data rows
    # Fields: id, title, artist, genre, year, duration, bpm, tags (mood+theme merged), popularity
    header = "id;title;artist;genre;yr;dur;bpm;tags;pop"
    candidate_lines = [header]
    for c in candidates:
        dur = ""
        if c.get("duration") is not None:
            m, s = divmod(int(c["duration"]), 60)
            dur = f"{m}:{s:02d}"
        # Merge mood + theme tags into single field, strip confidence scores
        mood = _strip_scores(c.get("mood_tags") or "")
        theme = _strip_scores(c.get("theme_tags") or "")
        tags = ",".join(filter(None, [mood, theme]))
        parts = [
            str(c["id"]),
            c["title"],
            c["artist"],
            c.get("genre") or "",
            str(c["year"]) if c.get("year") else "",
            dur,
            str(c["bpm"]) if c.get("bpm") else "",
            tags,
            str(c["popularity"]) if c.get("popularity") is not None else "",
        ]
        candidate_lines.append(";".join(parts))

    duration_note = ""
    if target_duration_min:
        duration_note = f"\nTarget duration: {target_duration_min}min (±5min). Use dur column to track total."

    user_msg = f"""Prompt: "{prompt}"
Select {max_songs} songs.{duration_note}

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
