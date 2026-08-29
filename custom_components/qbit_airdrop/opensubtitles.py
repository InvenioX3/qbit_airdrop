from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://api.opensubtitles.com/api/v1"
_USER_AGENT = "qbit_airdrop v1.0.0"
_TIMEOUT = 15

# Identifies this integration itself as the API "consumer" — fixed, never
# exposed to or entered by the end user, doesn't expire. Distinct from the
# per-user login token below, which is obtained from the end user's own
# OpenSubtitles account credentials (entered in the options panel) and is
# only valid for 24 hours.
_API_KEY = "XspMdbf1G5ac39ksCcWRWQhECYc94fxR"

_HEADERS_BASE = {"Api-Key": _API_KEY, "User-Agent": _USER_AGENT}

# The daily download quota (20/day free, up to 1000/day VIP) belongs to the
# end user's own account, not this app's API key — each user of this
# integration burns their own allotment independently. 429 is the
# conventional HTTP status for "quota/rate limit exceeded," but this hasn't
# been confirmed against a real exhausted-quota response yet — worth
# checking the logged status/body here against what actually comes back.


class QuotaExceededError(Exception):
    """Raised when OpenSubtitles reports the account's quota is exhausted."""

# Built against OpenSubtitles.com's documented v1 REST API. Endpoint paths,
# field names, and the exact response shape haven't been exercised against
# a live account in this session — treat this module as needing a real
# end-to-end test before relying on it, the same way the mkvmerge -J field
# names needed one live run to confirm. Logged at warning level throughout
# specifically so a real test run's request/response shape is visible
# without needing debug logging enabled.


async def login(session, username: str, password: str) -> str | None:
    """Returns a bearer token on success, None on failure."""
    headers = {**_HEADERS_BASE, "Content-Type": "application/json"}
    try:
        async with session.post(
            f"{_BASE_URL}/login",
            json={"username": username, "password": password},
            headers=headers,
            timeout=_TIMEOUT,
        ) as resp:
            body_text = await resp.text()
            if resp.status != 200:
                _LOGGER.warning(
                    "[QBIT] opensubtitles: login failed status=%s body=%s",
                    resp.status, body_text[:300],
                )
                return None
            data = await resp.json(content_type=None)
            token = data.get("token")
            if not token:
                _LOGGER.warning("[QBIT] opensubtitles: login response had no token body=%s", body_text[:300])
                return None
            _LOGGER.warning("[QBIT] opensubtitles: login succeeded username=%s", username)
            return token
    except Exception:
        _LOGGER.exception("[QBIT] opensubtitles: login request error")
        return None


async def search(
    session,
    token: str,
    *,
    query: str,
    language: str,
    year: int | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> dict | None:
    """Searches by title (plus year for movies, or show+season/episode for
    TV) and the target subtitle language, sorted by download count so the
    most community-validated result is used. Returns the top match's raw
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

    headers = {**_HEADERS_BASE, "Authorization": f"Bearer {token}"}

    _LOGGER.warning("[QBIT] opensubtitles: search params=%s", params)

    try:
        async with session.get(
            f"{_BASE_URL}/subtitles", params=params, headers=headers, timeout=_TIMEOUT,
        ) as resp:
            body_text = await resp.text()
            if resp.status == 429:
                _LOGGER.warning(
                    "[QBIT] opensubtitles: quota/rate limit hit (429) during search body=%s",
                    body_text[:300],
                )
                raise QuotaExceededError()
            if resp.status != 200:
                _LOGGER.warning(
                    "[QBIT] opensubtitles: search failed status=%s params=%s body=%s",
                    resp.status, params, body_text[:300],
                )
                return None
            data = await resp.json(content_type=None)
    except QuotaExceededError:
        raise
    except Exception:
        _LOGGER.exception("[QBIT] opensubtitles: search request error params=%s", params)
        return None

    results = data.get("data") or []
    _LOGGER.warning("[QBIT] opensubtitles: search returned %d result(s) params=%s", len(results), params)
    if not results:
        return None

    top = results[0]
    _LOGGER.warning("[QBIT] opensubtitles: top match attributes=%s", top.get("attributes"))
    return top.get("attributes")


async def request_download(session, token: str, file_id: int) -> str | None:
    """Consumes one unit of the account's download quota; returns the
    signed, time-limited direct download URL, or None on failure."""
    headers = {**_HEADERS_BASE, "Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    try:
        async with session.post(
            f"{_BASE_URL}/download",
            json={"file_id": file_id},
            headers=headers,
            timeout=_TIMEOUT,
        ) as resp:
            body_text = await resp.text()
            if resp.status == 429:
                _LOGGER.warning(
                    "[QBIT] opensubtitles: quota exceeded (429) requesting download file_id=%s body=%s",
                    file_id, body_text[:300],
                )
                raise QuotaExceededError()
            if resp.status != 200:
                _LOGGER.warning(
                    "[QBIT] opensubtitles: download request failed status=%s file_id=%s body=%s",
                    resp.status, file_id, body_text[:300],
                )
                return None
            data = await resp.json(content_type=None)
            link = data.get("link")
            remaining = data.get("remaining")
            _LOGGER.warning(
                "[QBIT] opensubtitles: download link obtained file_id=%s remaining_quota=%s link=%s",
                file_id, remaining, link,
            )
            return link
    except QuotaExceededError:
        raise
    except Exception:
        _LOGGER.exception("[QBIT] opensubtitles: download request error file_id=%s", file_id)
        return None
