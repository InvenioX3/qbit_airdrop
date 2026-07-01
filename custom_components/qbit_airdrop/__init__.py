from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse, parse_qs
import asyncio
import os
import re

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import aiohttp_client

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
    CONF_BASE_PATH,
)

# ---------- base URL from legacy host/port (no auth) ----------
def _resolve_base(entry: ConfigEntry) -> Tuple[str]:
    d = entry.options or entry.data or {}
    host = (d.get(CONF_HOST) or "").strip().strip("/")
    port = int(d.get(CONF_PORT) or 8080)

    if not host:
        return ("",)

    if "://" not in host:
        base = f"http://{host}:{port}"
    else:
        parsed = urlparse(host)
        netloc = parsed.netloc or parsed.path
        if ":" in netloc:
            base = f"{parsed.scheme}://{netloc}"
        else:
            base = f"{parsed.scheme}://{netloc}:{port}"
    return (base.rstrip("/"),)


def _resolve_base_path(entry: ConfigEntry) -> str:
    d = entry.options or entry.data or {}
    base_path = (d.get(CONF_BASE_PATH) or "").strip()
    return base_path
    
def _season_from_magnet(magnet: str) -> str:
    try:
        query = magnet.split("?", 1)[1]
        dn = parse_qs(query).get("dn", [""])[0]
    except Exception:
        return ""

    dn = dn.replace("+", " ")

    m = re.search(r"\b(S\d{1,2})E\d{1,3}\b", dn, re.I)
    if m:
        return m.group(1).upper()

    m = re.search(r"\b(S\d{1,2})\b(?!-\d)", dn, re.I)
    if m:
        return m.group(1).upper()

    return ""
    
def _episode_filename_from_magnet(magnet: str) -> str:
    try:
        query = magnet.split("?", 1)[1]
        dn = parse_qs(query).get("dn", [""])[0]
    except Exception:
        return ""

    dn = dn.replace("+", " ")

    m = re.search(r"\b(S\d{1,2}E\d{1,3})\b", dn, re.I)
    if not m:
        return ""

    show = dn[:m.start()]

    show = re.sub(r"\s*\(?\b(?:19|20)\d{2}\b\)?\s*", " ", show)
    show = re.sub(r"[._]+", " ", show)
    show = show.strip(" ._-")

    return f"{show} {m.group(1).upper()}".strip()
    
def _hash_from_magnet(magnet: str) -> str:
    m = re.search(
        r"xt=urn:btih:([a-fA-F0-9]+)",
        magnet,
        re.I,
    )

    return m.group(1).lower() if m else ""

async def async_setup(hass: HomeAssistant, config) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # HTTP views
    from .http import QbitAirdropActiveView, QbitAirdropDeleteView

    hass.http.register_view(QbitAirdropActiveView(hass, entry))
    hass.http.register_view(QbitAirdropDeleteView(hass, entry))

    session = aiohttp_client.async_get_clientsession(hass)
    pending_renames: dict[str, dict] = {}
    queue_task = None

    async def process_torrent(
        base: str,
        torrent_hash: str,
        rename_name: str,
        season: str,
    ) -> None:

        try:
            files = []

            for _ in range(60):
                async with session.get(
                    f"{base}/api/v2/torrents/files",
                    params={"hash": torrent_hash},
                    timeout=10,
                ) as files_resp:
                    files = await files_resp.json()

                if files:
                    break

                await asyncio.sleep(1)

            if not files:
                return False

            video_exts = {
                ".mkv",
                ".mp4",
                ".avi",
                ".m4v",
                ".mov",
                ".ts",
                ".m2ts",
                ".wmv",
            }

            best = None

            for f in files:
                path = str(f.get("name", ""))
                ext = os.path.splitext(path)[1].lower()

                if ext not in video_exts:
                    continue

                if (
                    best is None or
                    f.get("size", 0) > best.get("size", 0)
                ):
                    best = f

            if best and rename_name:
                old_path = best["name"]
                ext = os.path.splitext(old_path)[1]

                if "/" in old_path:
                    folder = old_path.rsplit("/", 1)[0]
                    new_path = f"{folder}/{rename_name}{ext}"
                else:
                    new_path = f"{rename_name}{ext}"

                await session.post(
                    f"{base}/api/v2/torrents/renameFile",
                    data={
                        "hash": torrent_hash,
                        "oldPath": old_path,
                        "newPath": new_path,
                    },
                    timeout=10,
                )

            folder_source = None

            if best:
                folder_source = best["name"]

            if season and folder_source and "/" in folder_source:
                root_folder = folder_source.split("/", 1)[0]

                await session.post(
                    f"{base}/api/v2/torrents/renameFolder",
                    data={
                        "hash": torrent_hash,
                        "oldPath": root_folder,
                        "newPath": season,
                    },
                    timeout=10,
                )

        except Exception:
            return False

        return True
        
    async def process_pending_queue() -> None:
        while True:

            for torrent_hash, item in list(
                pending_renames.items()
            ):
                ok = await process_torrent(
                    item["base"],
                    torrent_hash,
                    item["rename_name"],
                    item["season"],
                )

                if ok:
                    pending_renames.pop(
                        torrent_hash,
                        None,
                    )
                    continue

                item["attempts"] += 1

                if item["attempts"] > 1440:
                    pending_renames.pop(
                        torrent_hash,
                        None,
                    )

            await asyncio.sleep(60)
            
    queue_task = hass.async_create_task(
        process_pending_queue()
    )

    async def add_magnet(call: ServiceCall) -> None:
        data = call.data or {}
        magnet = (data.get("magnet") or "").strip()
        category = (data.get("category") or "").strip()
        clean_title = (data.get("clean_title") or "").strip()

        res = (data.get("res") or "").strip()
        codec = (data.get("codec") or "").strip()
        audio = (data.get("audio") or "").strip()
        if not magnet:
            return

        (base,) = _resolve_base(entry)
        if not base:
            return

        base_path = _resolve_base_path(entry)
        season = _season_from_magnet(magnet)
        torrent_hash = _hash_from_magnet(magnet)

        media_parts = [
            p for p in (res, codec, audio)
            if p
        ]

        clean_title = re.sub(
            r'[<>:"/\\|?*]',
            "",
            clean_title,
        )

        rename_name = clean_title

        if media_parts:
            rename_name = (
                f"{clean_title} "
                f"[{' • '.join(media_parts)}]"
            )

        savepath = ""
        if category and base_path:
            # Join base_path + category, ensure trailing slash
            sep_needed = not (base_path.endswith("/") or base_path.endswith("\\"))
            savepath = f"{base_path}{'/' if sep_needed else ''}{category}"

            if season:
                savepath = f"{savepath}/{season}"
            
            if not (savepath.endswith("/") or savepath.endswith("\\")):
                savepath = f"{savepath}/"
            try:
                async with session.post(
                    f"{base}/api/v2/torrents/createCategory",
                    data={"name": category, "savePath": savepath},
                    timeout=10,
                ) as resp:
                    await resp.text()
                    # ignore status; on some builds createCategory returns 409 if exists
            except Exception:
                pass

        # Add magnet (include category/savepath if we have them)
        form = {"urls": magnet}
        if category:
            form["category"] = category
        if savepath:
            form["savepath"] = savepath

        try:
            async with session.post(
                f"{base}/api/v2/torrents/add",
                data=form,
                timeout=20,
            ) as resp:
                await resp.text()  # quiet behavior

                if torrent_hash and (
                    rename_name or
                    season
                ):
                    pending_renames[torrent_hash] = {
                        "base": base,
                        "rename_name": rename_name,
                        "season": season,
                        "attempts": 0,
                    }
                    
        except Exception:
            pass

    async def reload_entry(call: ServiceCall) -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    hass.services.async_register(DOMAIN, "add_magnet", add_magnet)
    hass.services.async_register(DOMAIN, "reload_entry", reload_entry)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        hass.services.async_remove(DOMAIN, "add_magnet")
        hass.services.async_remove(DOMAIN, "reload_entry")
    except Exception:
        pass
    return True
