from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import timedelta
from typing import Tuple
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
    CONF_BASE_PATH,
)

_LOGGER = logging.getLogger(__name__)

_POLL_INTERVAL = timedelta(seconds=15)

_BTIH_HEX_RE = re.compile(r"btih:([A-Fa-f0-9]{40})")
_BTIH_B32_RE = re.compile(r"btih:([A-Za-z2-7]{32})")

_SEASON_TOKEN_RE = re.compile(r"\bS(\d{1,2})\b(?!-\d)", re.I)
_SEASON_WORD_RE = re.compile(r"\bSeason\s*(\d{1,2})\b", re.I)
_EPISODE_TOKEN_RE = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.I)

_VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv",
}


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


def _resolve_base_path(entry: ConfigEntry) -> str:
    data = entry.options or entry.data or {}
    return (data.get(CONF_BASE_PATH) or "").strip()


def _extract_hash(magnet: str) -> str:
    match = _BTIH_HEX_RE.search(magnet)
    if match:
        return match.group(1).lower()

    match = _BTIH_B32_RE.search(magnet)
    if match:
        return base64.b32decode(match.group(1).upper()).hex()

    return ""


def _is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _VIDEO_EXTS


def _detect_season(name: str) -> str:
    match = _SEASON_TOKEN_RE.search(name)
    if not match:
        match = _SEASON_WORD_RE.search(name)
    return f"S{int(match.group(1)):02d}" if match else ""


def _detect_episode(name: str) -> str:
    match = _EPISODE_TOKEN_RE.search(name)
    if not match:
        return ""
    season_num, episode_num = match.groups()
    return f"S{int(season_num):02d}E{int(episode_num):02d}"


def _file_in_season_folder(path: str) -> bool:
    if "/" not in path:
        return False
    parent_leaf = path.rsplit("/", 1)[0].rsplit("/", 1)[-1]
    return bool(_detect_season(parent_leaf))


def _root_folder(folders: list[str]) -> str:
    return next((f for f in folders if "/" not in f), "")


def _sibling_path(path: str, new_name: str) -> str:
    if "/" in path:
        parent = path.rsplit("/", 1)[0]
        return f"{parent}/{new_name}"
    return new_name


def _build_location(base_path: str, *parts: str) -> str:
    normalized = base_path.strip().replace("/", "\\").rstrip("\\")
    segments = [normalized] + [p.strip("\\/ ") for p in parts if p]
    return "\\".join(segments) + "\\"


async def _fetch_index(session, base: str, torrent_hash: str) -> dict | None:
    try:
        async with session.get(
            f"{base}/api/v2/torrents/files",
            params={"hash": torrent_hash},
            timeout=15,
        ) as resp:
            if resp.status != 200:
                return None
            files_raw = await resp.json(content_type=None)
    except Exception:
        _LOGGER.exception("[QBIT] fetch index request error hash=%s", torrent_hash)
        return None

    if not files_raw:
        return None

    files = []
    folders = set()

    for entry in files_raw:
        path = str(entry.get("name") or "")
        if not path:
            continue

        files.append({
            "id": entry.get("index"),
            "path": path,
            "size": entry.get("size"),
        })

        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))

    return {
        "files": files,
        "folders": sorted(folders),
    }


async def _rename_folder(session, base, torrent_hash, old_path, new_path) -> bool:
    if not old_path or not new_path or old_path == new_path:
        _LOGGER.warning(
            "[QBIT] renameFolder skipped old=%r new=%r",
            old_path, new_path,
        )
        return True

    try:
        async with session.post(
            f"{base}/api/v2/torrents/renameFolder",
            data={"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
            timeout=10,
        ) as resp:
            if resp.status >= 400:
                _LOGGER.warning(
                    "[QBIT] renameFolder failed status=%s old=%s new=%s",
                    resp.status, old_path, new_path,
                )
                return False
    except Exception:
        _LOGGER.exception(
            "[QBIT] renameFolder request error old=%s new=%s",
            old_path, new_path,
        )
        return False

    _LOGGER.warning(
        "[QBIT] renameFolder ok old=%s new=%s",
        old_path, new_path,
    )
    return True


async def _rename_file(session, base, torrent_hash, old_path, new_path) -> bool:
    if not old_path or not new_path or old_path == new_path:
        _LOGGER.warning(
            "[QBIT] renameFile skipped old=%r new=%r",
            old_path, new_path,
        )
        return True

    try:
        async with session.post(
            f"{base}/api/v2/torrents/renameFile",
            data={"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
            timeout=10,
        ) as resp:
            if resp.status >= 400:
                _LOGGER.warning(
                    "[QBIT] renameFile failed status=%s old=%s new=%s",
                    resp.status, old_path, new_path,
                )
                return False
    except Exception:
        _LOGGER.exception(
            "[QBIT] renameFile request error old=%s new=%s",
            old_path, new_path,
        )
        return False

    _LOGGER.warning(
        "[QBIT] renameFile ok old=%s new=%s",
        old_path, new_path,
    )
    return True


async def _set_location(session, base, torrent_hash, location) -> bool:
    if not location:
        return True

    try:
        async with session.post(
            f"{base}/api/v2/torrents/setLocation",
            data={"hashes": torrent_hash, "location": location},
            timeout=10,
        ) as resp:
            if resp.status >= 400:
                _LOGGER.warning(
                    "[QBIT] setLocation failed status=%s location=%s",
                    resp.status, location,
                )
                return False
    except Exception:
        _LOGGER.exception(
            "[QBIT] setLocation request error location=%s",
            location,
        )
        return False

    return True


async def _set_file_priority(session, base, torrent_hash, file_ids, priority) -> bool:
    if not file_ids:
        return True

    try:
        async with session.post(
            f"{base}/api/v2/torrents/filePrio",
            data={
                "hash": torrent_hash,
                "id": "|".join(str(i) for i in file_ids),
                "priority": priority,
            },
            timeout=10,
        ) as resp:
            if resp.status >= 400:
                _LOGGER.warning(
                    "[QBIT] filePrio failed status=%s ids=%s",
                    resp.status, file_ids,
                )
                return False
    except Exception:
        _LOGGER.exception("[QBIT] filePrio request error ids=%s", file_ids)
        return False

    return True


async def _start_torrent(session, base, torrent_hash) -> bool:
    try:
        async with session.post(
            f"{base}/api/v2/torrents/start",
            data={"hashes": torrent_hash},
            timeout=10,
        ) as resp:
            if resp.status >= 400:
                _LOGGER.warning(
                    "[QBIT] start failed status=%s hash=%s",
                    resp.status, torrent_hash,
                )
                return False
    except Exception:
        _LOGGER.exception("[QBIT] start request error hash=%s", torrent_hash)
        return False

    return True


async def _apply_file_priorities(session, base, torrent_hash, files, keep_ids) -> None:
    drop_ids = [f["id"] for f in files if f["id"] not in keep_ids]
    await _set_file_priority(session, base, torrent_hash, drop_ids, 0)


async def _process_queue_item(session, base, base_path, torrent_hash, meta, index) -> None:
    token_type = meta["token_type"]
    category = meta["category"]
    season = meta["season"]
    rename_name = meta["rename_name"]

    files = index["files"]
    folders = index["folders"]
    root_folder = _root_folder(folders)

    videos = [f for f in files if _is_video(f["path"])]
    largest = max(videos, key=lambda f: f["size"]) if videos else None

    _LOGGER.warning(
        "[QBIT] process hash=%s token_type=%r category=%r videos=%s largest=%r root_folder=%r",
        torrent_hash, token_type, category, len(videos),
        largest["path"] if largest else None, root_folder,
    )

    if not category:
        # Movie (token_type "year", or unclassified — no season signal at all)
        if largest:
            ext = os.path.splitext(largest["path"])[1]
            new_path = (
                f"{root_folder}/{rename_name}{ext}"
                if root_folder else f"{rename_name}{ext}"
            )
            await _rename_file(session, base, torrent_hash, largest["path"], new_path)

        if root_folder:
            await _rename_folder(session, base, torrent_hash, root_folder, rename_name)

        keep_ids = {largest["id"]} if largest else set()
        await _apply_file_priorities(session, base, torrent_hash, files, keep_ids)

    elif token_type == "se":
        if largest:
            ext = os.path.splitext(largest["path"])[1]
            new_path = (
                f"{root_folder}/{rename_name}{ext}"
                if root_folder else f"{rename_name}{ext}"
            )
            await _rename_file(session, base, torrent_hash, largest["path"], new_path)

        if root_folder:
            await _rename_folder(session, base, torrent_hash, root_folder, season)
            location = _build_location(base_path, category)
        else:
            location = _build_location(base_path, category, season)

        keep_ids = {largest["id"]} if largest else set()
        await _apply_file_priorities(session, base, torrent_hash, files, keep_ids)
        await _set_location(session, base, torrent_hash, location)

    elif token_type in ("s", "season"):
        keep_ids = {f["id"] for f in videos}

        for f in videos:
            episode = _detect_episode(os.path.basename(f["path"]))
            if not episode:
                continue
            ext = os.path.splitext(f["path"])[1]
            new_path = _sibling_path(f["path"], f"{category} {episode}{ext}")
            await _rename_file(session, base, torrent_hash, f["path"], new_path)

        if root_folder:
            await _rename_folder(session, base, torrent_hash, root_folder, season)

        await _apply_file_priorities(session, base, torrent_hash, files, keep_ids)
        await _set_location(session, base, torrent_hash, _build_location(base_path, category))

    elif token_type == "complete":
        keep_ids = {f["id"] for f in videos if _file_in_season_folder(f["path"])}

        for f in videos:
            if f["id"] not in keep_ids:
                continue
            episode = _detect_episode(os.path.basename(f["path"]))
            if not episode:
                continue
            ext = os.path.splitext(f["path"])[1]
            new_path = _sibling_path(f["path"], f"{category} {episode}{ext}")
            await _rename_file(session, base, torrent_hash, f["path"], new_path)

        # Rename nested season folders first — root rename happens last so
        # their currently-indexed paths (still prefixed by the old root name)
        # stay valid when renameFolder is called.
        for folder in folders:
            if folder == root_folder:
                continue
            leaf = folder.rsplit("/", 1)[-1]
            normalized = _detect_season(leaf)
            if not normalized or normalized == leaf:
                continue
            parent = folder.rsplit("/", 1)[0]
            new_path = f"{parent}/{normalized}"
            await _rename_folder(session, base, torrent_hash, folder, new_path)

        if root_folder:
            await _rename_folder(session, base, torrent_hash, root_folder, category)

        await _apply_file_priorities(session, base, torrent_hash, files, keep_ids)

        # Root folder was just renamed to `category` itself — setLocation only
        # needs base_path, or the move produces base_path/category/category/...
        location = (
            _build_location(base_path)
            if root_folder else _build_location(base_path, category)
        )
        await _set_location(session, base, torrent_hash, location)

    else:
        _LOGGER.warning(
            "[QBIT] unrecognized token_type=%s hash=%s — skipping rename pipeline",
            token_type, torrent_hash,
        )

    await _start_torrent(session, base, torrent_hash)


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

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "queue": {},
    }

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

            status_ok = resp.status == 200

        if not status_ok:
            return

        torrent_hash = ""
        try:
            payload = json.loads(body)
            added = payload.get("added_torrent_ids") or []
            if added:
                torrent_hash = str(added[0]).strip().lower()
        except (ValueError, AttributeError):
            pass

        if not torrent_hash:
            torrent_hash = _extract_hash(magnet)

        if not torrent_hash:
            return

        _LOGGER.warning(
            "[QBIT] add_magnet queued hash=%s",
            torrent_hash,
        )

        hass.data[DOMAIN][entry.entry_id]["queue"][torrent_hash] = {
            "category": category,
            "clean_title": (data.get("clean_title") or "").strip(),
            "rename_name": (data.get("rename_name") or "").strip(),
            "token_type": (data.get("token_type") or "").strip(),
            "season": (data.get("season") or "").strip(),
            "res": (data.get("res") or "").strip(),
            "codec": (data.get("codec") or "").strip(),
            "audio": (data.get("audio") or "").strip(),
        }

    async def reload_entry(call: ServiceCall) -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    async def _poll_queue(now) -> None:
        store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not store:
            return

        queue = store["queue"]
        if not queue:
            return

        base, = _resolve_base(entry)
        if not base:
            return

        base_path = _resolve_base_path(entry)

        for torrent_hash, meta in list(queue.items()):
            index = await _fetch_index(session, base, torrent_hash)
            if index is None:
                continue

            try:
                await _process_queue_item(
                    session, base, base_path, torrent_hash, meta, index,
                )
            except Exception:
                _LOGGER.exception(
                    "[QBIT] queue processing failed hash=%s",
                    torrent_hash,
                )
                continue

            queue.pop(torrent_hash, None)

    unsub = async_track_time_interval(hass, _poll_queue, _POLL_INTERVAL)
    hass.data[DOMAIN][entry.entry_id]["unsub_poll"] = unsub

    hass.services.async_register(
        DOMAIN,
        "add_magnet",
        add_magnet,
    )

    hass.services.async_register(
        DOMAIN,
        "reload_entry",
        reload_entry,
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

    hass.services.async_remove(
        DOMAIN,
        "reload_entry",
    )

    store = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if store is not None:
        unsub = store.get("unsub_poll")
        if unsub is not None:
            unsub()

    return True