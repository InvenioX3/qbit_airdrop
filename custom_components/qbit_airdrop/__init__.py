from __future__ import annotations

import logging
from typing import Tuple
from urllib.parse import urlparse
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import aiohttp_client
import base64
import re

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
)

_LOGGER = logging.getLogger(__name__)


def _resolve_base(entry: ConfigEntry) -> Tuple[str]:
    data = entry.options or entry.data or {}

    host = (
        data.get(CONF_HOST)
        or ""
    ).strip().strip("/")

    port = int(
        data.get(CONF_PORT)
        or 8080
    )

    if not host:
        return ("",)

    if "://" not in host:
        return (f"http://{host}:{port}",)

    parsed = urlparse(host)
    netloc = parsed.netloc or parsed.path

    if ":" in netloc:
        return (f"{parsed.scheme}://{netloc}".rstrip("/"),)

    return (f"{parsed.scheme}://{netloc}:{port}".rstrip("/"),)

_BTIH_HEX_RE = re.compile(r"btih:([A-Fa-f0-9]{40})")
_BTIH_B32_RE = re.compile(r"btih:([A-Za-z2-7]{32})")


def _extract_hash(magnet: str) -> str:
    match = _BTIH_HEX_RE.search(magnet)
    if match:
        return match.group(1).lower()

    match = _BTIH_B32_RE.search(magnet)
    if match:
        return base64.b32decode(match.group(1).upper()).hex()

    return ""


async def _fetch_index(session, base: str, torrent_hash: str) -> dict | None:
    async with session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=15,
    ) as resp:
        if resp.status != 200:
            return None
        files_raw = await resp.json(content_type=None)

    if not files_raw:
        return None

    async with session.get(
        f"{base}/api/v2/torrents/info",
        params={"hashes": torrent_hash},
        timeout=15,
    ) as resp:
        info_raw = await resp.json(content_type=None) if resp.status == 200 else []

    save_path = str(info_raw[0].get("save_path") or "") if info_raw else ""

    files = []
    folders = set()

    for entry in files_raw:
        path = str(entry.get("name") or "")
        if not path:
            continue

        files.append({
            "path": path,
            "size": entry.get("size"),
        })

        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))

    return {
        "hash": torrent_hash,
        "save_path": save_path,
        "files": files,
        "folders": sorted(folders),
    }

async def async_setup(
    hass: HomeAssistant,
    config,
) -> bool:
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    from .http import QbitAirdropActiveView, QbitAirdropDeleteView

    hass.http.register_view(
        QbitAirdropActiveView(
            hass,
            entry,
        )
    )

    hass.http.register_view(
        QbitAirdropDeleteView(
            hass,
            entry,
        )
    )

    session = aiohttp_client.async_get_clientsession(hass)

    async def add_magnet(call: ServiceCall) -> None:
        data = call.data or {}

        magnet = (
            data.get("magnet")
            or ""
        ).strip()

        if not magnet:
            return

        base, = _resolve_base(entry)

        if not base:
            return

        form = {
            "urls": magnet,
        }

        category = (
            data.get("category")
            or ""
        ).strip()

        if category:
            form["category"] = category

        async with session.post(
            f"{base}/api/v2/torrents/add",
            data=form,
            timeout=20,
        ) as resp:
            body = await resp.text()

            _LOGGER.warning(
                "[QBIT] add_magnet status=%s body=%s",
                resp.status,
                body[:200],
            )

    hass.services.async_register(
        DOMAIN,
        "add_magnet",
        add_magnet,
    )

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    hass.services.async_remove(
        DOMAIN,
        "add_magnet",
    )

    return True