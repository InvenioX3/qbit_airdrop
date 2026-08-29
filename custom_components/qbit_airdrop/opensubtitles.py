from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://api.opensubtitles.com/api/v1"
_USER_AGENT = "qbit_airdrop v1.0.0"
_TIMEOUT = 15

# Built against OpenSubtitles.com's documented v1 REST API. Endpoint paths,
# field names, and the exact response shape haven't been exercised against
# a live account/API key in this session — treat this module as needing a
# real end-to-end test before relying on it, the same way the mkvmerge -J
# field names needed one live run to confirm.


async def login(session, api_key: str, username: str, password: str) -> str | None:
    """Returns a bearer token on success, None on failure."""
    headers = {"Api-Key": api_key, "User-Agent": _USER_AGENT, "Content-Type": "application/json"}
    try:
        async with session.post(
            f"{_BASE_URL}/login",
            json={"username": username, "password": password},
            headers=headers,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning("[QBIT] opensubtitles: login failed status=%s", resp.status)
                return None
            data = await resp.json(content_type=None)
            token = data.get("token")
            if not token:
                _LOGGER.warning("[QBIT] opensubtitles: login response had no token")
            return token
    except Exception:
        _LOGGER.exception("[QBIT] opensubtitles: login request error")
        return None


async def search(
    session,
    api_key: str,
    token: str,
    *,
    query: str,
    language: str,
    year: int | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> dict | None:
    """Searches by title (plus year for movies, or season/episode for TV)
    and the target subtitle language, sorted by download count so the most
    community-validated result is used. Returns the top match's raw
    attributes dict (including "files", needed for request_download), or
    None if nothing matched.

    Uses OpenSubtitles' own structured filter parameters (year,
    season_number, episode_number) rather than folding everything into the
    free-text query — those are documented as exact filters, not subject to
    the kind of query relaxation a plain text search can do."""
    params = {
        "query": query,
        "languages": language,
        "order_by": "download_count",
        "order_direction": "desc",
    }
    if year is not None:
        params["year"] = str(year)
    if season_number is not None:
        params["season_number"] = str(season_number)
    if episode_number is not None:
        params["episode_number"] = str(episode_number)

    headers = {
        "Api-Key": api_key,
        "User-Agent": _USER_AGENT,
        "Authorization": f"Bearer {token}",
    }

    try:
        async with session.get(
            f"{_BASE_URL}/subtitles", params=params, headers=headers, timeout=_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning(
                    "[QBIT] opensubtitles: search failed status=%s query=%r language=%s",
                    resp.status, query, language,
                )
                return None
            data = await resp.json(content_type=None)
    except Exception:
        _LOGGER.exception("[QBIT] opensubtitles: search request error query=%r", query)
        return None

    results = data.get("data") or []
    if not results:
        _LOGGER.debug(
            "[QBIT] opensubtitles: no results query=%r year=%s language=%s",
            query, year, language,
        )
        return None

    top = results[0]
    _LOGGER.debug(
        "[QBIT] opensubtitles: top match query=%r language=%s attributes=%s",
        query, language, top.get("attributes"),
    )
    return top.get("attributes")


async def request_download(session, api_key: str, token: str, file_id: int) -> str | None:
    """Consumes one unit of the account's download quota; returns the
    signed, time-limited direct download URL, or None on failure."""
    headers = {
        "Api-Key": api_key,
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    try:
        async with session.post(
            f"{_BASE_URL}/download",
            json={"file_id": file_id},
            headers=headers,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning(
                    "[QBIT] opensubtitles: download request failed status=%s file_id=%s",
                    resp.status, file_id,
                )
                return None
            data = await resp.json(content_type=None)
            link = data.get("link")
            remaining = data.get("remaining")
            _LOGGER.debug(
                "[QBIT] opensubtitles: download link obtained file_id=%s remaining_quota=%s",
                file_id, remaining,
            )
            return link
    except Exception:
        _LOGGER.exception("[QBIT] opensubtitles: download request error file_id=%s", file_id)
        return None
