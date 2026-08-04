from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
from datetime import timedelta
from urllib.parse import unquote_plus

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_time_interval, async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_BASE_PATH,
    CONF_DOWNLOAD_PATH,
    CONF_MOVIE_PATH,
    CONF_TEMP_HA_PATH,
    CONF_CLEANUP_TIME,
)
from .util import resolve_base as _resolve_base

_LOGGER = logging.getLogger(__name__)

_POLL_INTERVAL = timedelta(seconds=15)
_COMMAND_DELAY = 0.25

# Torrent states qBittorrent considers "settled" — fully downloaded and past
# the completion-triggered move, whether currently seeding or not.
_COMPLETE_STATES = {"uploading", "stalledup", "forcedup"}

_LAGGARD_THRESHOLD = timedelta(minutes=10)
_LAGGARD_INTERVAL = timedelta(minutes=30)

_BTIH_HEX_RE = re.compile(r"btih:([A-Fa-f0-9]{40})")
_BTIH_B32_RE = re.compile(r"btih:([A-Za-z2-7]{32})")
_MAGNET_DN_RE = re.compile(r"[?&]dn=([^&]+)")
_INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_SEASON_TOKEN_RE = re.compile(r"\bS(\d{1,2})\b(?!-\d)", re.I)
_SEASON_WORD_RE = re.compile(r"\bSeason\s*(\d{1,2})\b", re.I)
_EPISODE_TOKEN_RE = re.compile(r"\bS(\d{1,2})((?:E\d{1,3})+)\b", re.I)
_EPISODE_NUM_RE = re.compile(r"E(\d{1,3})", re.I)

_VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv", ".iso",
}


def _resolve_base_path(entry: ConfigEntry) -> str:
    data = entry.options or entry.data or {}
    return (data.get(CONF_BASE_PATH) or "").strip()


def _resolve_download_path(entry: ConfigEntry) -> str:
    data = entry.options or entry.data or {}
    return (data.get(CONF_DOWNLOAD_PATH) or "").strip()


def _resolve_movie_path(entry: ConfigEntry) -> str:
    data = entry.options or entry.data or {}
    return (data.get(CONF_MOVIE_PATH) or "").strip()


def _resolve_temp_ha_path(entry: ConfigEntry) -> str:
    data = entry.options or entry.data or {}
    return (data.get(CONF_TEMP_HA_PATH) or "").strip()


def _resolve_cleanup_time(entry: ConfigEntry) -> tuple[int, int] | None:
    data = entry.options or entry.data or {}
    raw = (data.get(CONF_CLEANUP_TIME) or "").strip()
    if not raw:
        return None

    try:
        hour_str, minute_str = raw.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        _LOGGER.warning("[QBIT] invalid cleanup_time %r — cleanup schedule disabled", raw)
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        _LOGGER.warning("[QBIT] cleanup_time %r out of range — cleanup schedule disabled", raw)
        return None

    return hour, minute


def _folder_is_cleanup_safe(folder_path: str) -> bool:
    """True if folder_path contains nothing but .part files anywhere in its
    tree (or is empty) — safe to remove once its torrent shows complete."""
    for _root, _dirs, files in os.walk(folder_path):
        for fname in files:
            if not fname.lower().endswith(".part"):
                return False
    return True


def _cleanup_temp_folders_sync(temp_root: str, complete_names: set[str]) -> list[str]:
    """Blocking filesystem work — must be run via hass.async_add_executor_job.
    Returns the list of folder paths actually removed."""
    removed: list[str] = []
    try:
        entries = os.listdir(temp_root)
    except OSError:
        _LOGGER.exception("[QBIT] temp cleanup: could not list %s", temp_root)
        return removed

    for name in entries:
        if name not in complete_names:
            continue

        folder_path = os.path.join(temp_root, name)
        if not os.path.isdir(folder_path):
            continue
        if not _folder_is_cleanup_safe(folder_path):
            continue

        try:
            shutil.rmtree(folder_path)
            removed.append(folder_path)
        except OSError:
            _LOGGER.exception("[QBIT] temp cleanup: could not remove %s", folder_path)

    return removed


def _magnet_display_name(magnet: str) -> str:
    match = _MAGNET_DN_RE.search(magnet)
    if not match:
        return ""

    name = unquote_plus(match.group(1)).strip()
    name = _INVALID_PATH_CHARS_RE.sub(" ", name).strip(" .")
    return name


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
    season_num = int(match.group(1))
    episode_nums = [int(n) for n in _EPISODE_NUM_RE.findall(match.group(2))]
    episodes = "".join(f"E{n:02d}" for n in episode_nums)
    return f"S{season_num:02d}{episodes}"


def _file_in_season_folder(path: str) -> bool:
    if "/" not in path:
        return False
    parent_leaf = path.rsplit("/", 1)[0].rsplit("/", 1)[-1]
    return bool(_detect_season(parent_leaf))


def _root_folder(folders: list[str]) -> str:
    return next((f for f in folders if "/" not in f), "")


_BLURAY_MARKERS = {"bdmv", "!any", "certificate"}


def _is_bluray_structure(folders: list[str]) -> bool:
    for folder in folders:
        for segment in folder.split("/"):
            if segment.strip().lower() in _BLURAY_MARKERS:
                return True
    return False


def _is_due(meta: dict, now) -> bool:
    added_at = meta.get("added_at")
    if added_at is None:
        return True

    if now - added_at < _LAGGARD_THRESHOLD:
        return True

    last_checked_at = meta.get("last_checked_at")
    if last_checked_at is None:
        return True

    return now - last_checked_at >= _LAGGARD_INTERVAL


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


async def _cleanup_temp_folders(hass, session, base, temp_ha_path) -> None:
    if not temp_ha_path:
        return

    try:
        async with session.get(
            f"{base}/api/v2/torrents/info",
            params={"filter": "all"},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return
            live = await resp.json(content_type=None)
    except Exception:
        _LOGGER.exception("[QBIT] temp cleanup: torrent list fetch failed")
        return

    if not isinstance(live, list):
        return

    # The temp folder name and qBittorrent's own tracked torrent name are the
    # same string permanently — our renames only touch content inside that
    # folder, never the folder itself or qBittorrent's own tracked name.
    complete_names = {
        str(t.get("name") or "")
        for t in live
        if str(t.get("state") or "").lower() in _COMPLETE_STATES
    }
    if not complete_names:
        return

    removed = await hass.async_add_executor_job(
        _cleanup_temp_folders_sync, temp_ha_path, complete_names,
    )
    for path in removed:
        _LOGGER.debug("[QBIT] temp cleanup removed %s", path)


def _find_available_name(base_name: str, existing_names: set[str]) -> str:
    if base_name not in existing_names:
        return base_name
    n = 1
    while f"{base_name} ({n})" in existing_names:
        n += 1
    return f"{base_name} ({n})"


async def _collect_other_season_folders(session, base, torrent_hash, category) -> set[str]:
    """Root-level folder names of other currently-tracked torrents in the
    same category — the collision unit for TV is the season folder itself,
    since episodes move as a bundle within it."""
    try:
        async with session.get(
            f"{base}/api/v2/torrents/info",
            params={"filter": "all", "category": category},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return set()
            others = await resp.json(content_type=None)
    except Exception:
        _LOGGER.exception("[QBIT] dedup: same-category fetch failed")
        return set()

    if not isinstance(others, list):
        return set()

    names = set()
    for t in others:
        other_hash = str(t.get("hash") or "").lower()
        if other_hash == torrent_hash:
            continue
        other_index = await _fetch_index(session, base, other_hash)
        if not other_index:
            continue
        other_root = _root_folder(other_index["folders"])
        if other_root:
            names.add(other_root)
    return names


async def _collect_other_movie_filenames(session, base, torrent_hash) -> set[str]:
    """Video-file basenames (no extension) of other currently-tracked
    category-less torrents — movies land as individual files, so the
    collision unit is the file name itself, not a folder."""
    try:
        async with session.get(
            f"{base}/api/v2/torrents/info",
            params={"filter": "all", "category": ""},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return set()
            others = await resp.json(content_type=None)
    except Exception:
        _LOGGER.exception("[QBIT] dedup: movie fetch failed")
        return set()

    if not isinstance(others, list):
        return set()

    names = set()
    for t in others:
        other_hash = str(t.get("hash") or "").lower()
        if other_hash == torrent_hash:
            continue
        other_index = await _fetch_index(session, base, other_hash)
        if not other_index:
            continue
        for f in other_index["files"]:
            if _is_video(f["path"]):
                names.add(os.path.splitext(os.path.basename(f["path"]))[0])
    return names


async def _qbit_command(session, base, endpoint, data, *, timeout=10) -> bool:
    try:
        async with session.post(
            f"{base}/api/v2/torrents/{endpoint}",
            data=data,
            timeout=timeout,
        ) as resp:
            if resp.status >= 400:
                _LOGGER.warning(
                    "[QBIT] %s failed status=%s data=%s",
                    endpoint, resp.status, data,
                )
                return False
    except Exception:
        _LOGGER.exception("[QBIT] %s request error data=%s", endpoint, data)
        return False

    _LOGGER.debug("[QBIT] %s ok data=%s", endpoint, data)
    await asyncio.sleep(_COMMAND_DELAY)
    return True


async def _rename_folder(session, base, torrent_hash, old_path, new_path) -> bool:
    if not old_path or not new_path or old_path == new_path:
        _LOGGER.debug("[QBIT] renameFolder skipped old=%r new=%r", old_path, new_path)
        return True

    return await _qbit_command(
        session, base, "renameFolder",
        {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
        timeout=30,
    )


async def _rename_file(session, base, torrent_hash, old_path, new_path) -> bool:
    if not old_path or not new_path or old_path == new_path:
        _LOGGER.debug("[QBIT] renameFile skipped old=%r new=%r", old_path, new_path)
        return True

    return await _qbit_command(
        session, base, "renameFile",
        {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
        timeout=30,
    )


async def _set_location(session, base, torrent_hash, location) -> bool:
    if not location:
        return True

    return await _qbit_command(
        session, base, "setLocation",
        {"hashes": torrent_hash, "location": location},
        timeout=30,
    )


async def _set_file_priority(session, base, torrent_hash, file_ids, priority) -> bool:
    if not file_ids:
        return True

    return await _qbit_command(
        session, base, "filePrio",
        {
            "hash": torrent_hash,
            "id": "|".join(str(i) for i in file_ids),
            "priority": priority,
        },
    )


async def _start_torrent(session, base, torrent_hash) -> bool:
    return await _qbit_command(session, base, "start", {"hashes": torrent_hash})


async def _apply_file_priorities(session, base, torrent_hash, files, keep_ids) -> bool:
    drop_ids = [f["id"] for f in files if f["id"] not in keep_ids]
    return await _set_file_priority(session, base, torrent_hash, drop_ids, 0)


async def _rename_single_file_target(
    session, base, torrent_hash, files, largest, root_folder, folder_target, file_name,
    force_keep_all=False,
) -> bool:
    """Movie and single-episode ("se") torrents both boil down to: rename the
    one video file, rename its folder to `folder_target`, keep only that file."""
    ok = True

    if largest:
        ext = os.path.splitext(largest["path"])[1]
        new_path = (
            f"{root_folder}/{file_name}{ext}"
            if root_folder else f"{file_name}{ext}"
        )
        ok &= await _rename_file(session, base, torrent_hash, largest["path"], new_path)

    if root_folder:
        ok &= await _rename_folder(session, base, torrent_hash, root_folder, folder_target)

    keep_ids = (
        {f["id"] for f in files} if force_keep_all
        else ({largest["id"]} if largest else set())
    )
    ok &= await _apply_file_priorities(session, base, torrent_hash, files, keep_ids)

    return ok


async def _process_queue_item(session, base, base_path, movie_path, torrent_hash, meta, index) -> bool:
    token_type = meta["token_type"]
    category = meta["category"]
    season = meta["season"]
    rename_name = meta["rename_name"]

    files = index["files"]
    folders = index["folders"]
    root_folder = _root_folder(folders)

    videos = [f for f in files if _is_video(f["path"])]
    largest = max(videos, key=lambda f: f["size"]) if videos else None
    is_bluray = _is_bluray_structure(folders)

    _LOGGER.debug(
        "[QBIT] process hash=%s token_type=%r category=%r videos=%s largest=%r root_folder=%r is_bluray=%s",
        torrent_hash, token_type, category, len(videos),
        largest["path"] if largest else None, root_folder, is_bluray,
    )

    ok = True

    if not category:
        # Movie (token_type "year", or unclassified — no season signal at all).
        # Movies have no category, so unlike every other branch they were
        # never explicitly relocated — they just landed wherever qBittorrent's
        # own Default Save Path pointed. Explicitly relocate when a movie
        # path is configured.
        #
        # Movies land as individual files (no bundling benefit like a season
        # folder), so the dedup unit is the file name itself — check other
        # currently-tracked category-less torrents for a collision.
        other_names = await _collect_other_movie_filenames(session, base, torrent_hash)
        target_name = _find_available_name(rename_name, other_names)
        ok &= await _rename_single_file_target(
            session, base, torrent_hash, files, largest, root_folder,
            target_name, target_name, force_keep_all=is_bluray,
        )
        if movie_path:
            ok &= await _set_location(session, base, torrent_hash, _build_location(movie_path))

    elif token_type == "se":
        # TV episodes move as a bundle within their season folder, so the
        # dedup unit is that folder name — check other currently-tracked
        # same-category torrents for a collision.
        other_names = await _collect_other_season_folders(session, base, torrent_hash, category)
        season_target = _find_available_name(season, other_names)
        ok &= await _rename_single_file_target(
            session, base, torrent_hash, files, largest, root_folder,
            season_target, rename_name, force_keep_all=is_bluray,
        )
        location = (
            _build_location(base_path, category)
            if root_folder else _build_location(base_path, category, season_target)
        )
        ok &= await _set_location(session, base, torrent_hash, location)

    elif token_type in ("s", "season"):
        # Same dedup unit as "se" — the season folder, since a whole pack's
        # episodes move together within it.
        other_names = await _collect_other_season_folders(session, base, torrent_hash, category)
        season_target = _find_available_name(season, other_names)

        keep_ids = (
            {f["id"] for f in files} if is_bluray
            else {
                f["id"] for f in videos
                if _file_in_season_folder(f["path"])
                and _detect_episode(os.path.basename(f["path"]))
            }
        )

        for f in videos:
            if f["id"] not in keep_ids:
                _LOGGER.debug(
                    "[QBIT] episode rename skipped (not a recognized episode) path=%s",
                    f["path"],
                )
                continue
            episode = _detect_episode(os.path.basename(f["path"]))
            ext = os.path.splitext(f["path"])[1]
            new_path = _sibling_path(f["path"], f"{category} {episode}{ext}")
            ok &= await _rename_file(session, base, torrent_hash, f["path"], new_path)

        if root_folder:
            ok &= await _rename_folder(session, base, torrent_hash, root_folder, season_target)

        ok &= await _apply_file_priorities(session, base, torrent_hash, files, keep_ids)
        ok &= await _set_location(session, base, torrent_hash, _build_location(base_path, category))

    elif token_type == "complete":
        keep_ids = (
            {f["id"] for f in files} if is_bluray
            else {
                f["id"] for f in videos
                if _file_in_season_folder(f["path"])
                and _detect_episode(os.path.basename(f["path"]))
            }
        )

        for f in videos:
            if f["id"] not in keep_ids:
                _LOGGER.debug(
                    "[QBIT] episode rename skipped (not a recognized episode) path=%s",
                    f["path"],
                )
                continue
            episode = _detect_episode(os.path.basename(f["path"]))
            ext = os.path.splitext(f["path"])[1]
            new_path = _sibling_path(f["path"], f"{category} {episode}{ext}")
            ok &= await _rename_file(session, base, torrent_hash, f["path"], new_path)

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
            ok &= await _rename_folder(session, base, torrent_hash, folder, new_path)

        if root_folder:
            ok &= await _rename_folder(session, base, torrent_hash, root_folder, category)

        ok &= await _apply_file_priorities(session, base, torrent_hash, files, keep_ids)

        # Root folder was just renamed to `category` itself — setLocation only
        # needs base_path, or the move produces base_path/category/category/...
        location = (
            _build_location(base_path)
            if root_folder else _build_location(base_path, category)
        )
        ok &= await _set_location(session, base, torrent_hash, location)

    else:
        _LOGGER.warning(
            "[QBIT] unrecognized token_type=%s hash=%s — skipping rename pipeline",
            token_type, torrent_hash,
        )
        return True

    ok &= await _start_torrent(session, base, torrent_hash)
    return ok


async def async_setup(
    hass: HomeAssistant,
    config,
) -> bool:
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    from .http import (
        QbitAirdropActiveView,
        QbitAirdropDeleteView,
        QbitAirdropForceStartView,
        QbitAirdropStatsView,
    )

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

    hass.http.register_view(
        QbitAirdropForceStartView(
            hass,
            entry,
        )
    )

    hass.http.register_view(
        QbitAirdropStatsView(
            hass,
            entry,
        )
    )

    session = aiohttp_client.async_get_clientsession(hass)
    poll_lock = asyncio.Lock()

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

        download_path_base = _resolve_download_path(entry)
        if download_path_base:
            unique_name = _magnet_display_name(magnet) or _extract_hash(magnet)
            if unique_name:
                form["downloadPath"] = _build_location(download_path_base, unique_name)
                form["useDownloadPath"] = "true"

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

        magnet_hash = _extract_hash(magnet)

        if torrent_hash and magnet_hash and torrent_hash != magnet_hash:
            _LOGGER.warning(
                "[QBIT] hash mismatch added_torrent_ids=%s magnet_extracted=%s",
                torrent_hash, magnet_hash,
            )

        if not torrent_hash:
            torrent_hash = magnet_hash

        if not torrent_hash:
            return

        _LOGGER.debug(
            "[QBIT] add_magnet queued hash=%s",
            torrent_hash,
        )

        hass.data[DOMAIN][entry.entry_id]["queue"][torrent_hash] = {
            "category": category,
            "rename_name": (data.get("rename_name") or "").strip(),
            "token_type": (data.get("token_type") or "").strip(),
            "season": (data.get("season") or "").strip(),
            "added_at": dt_util.utcnow(),
            "last_checked_at": None,
        }

    async def flush_orphaned(call: ServiceCall) -> None:
        store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not store:
            return

        queue = store["queue"]
        if not queue:
            return

        base, = _resolve_base(entry)
        if not base:
            return

        try:
            async with session.get(
                f"{base}/api/v2/torrents/info",
                params={"filter": "all"},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return
                live = await resp.json(content_type=None)
        except Exception:
            _LOGGER.exception("[QBIT] flush_orphaned request error")
            return

        live_hashes = (
            {str(t.get("hash") or "").lower() for t in live}
            if isinstance(live, list) else set()
        )

        for torrent_hash in list(queue):
            if torrent_hash not in live_hashes:
                queue.pop(torrent_hash, None)
                _LOGGER.debug("[QBIT] flush_orphaned removed hash=%s", torrent_hash)

    async def _poll_queue(now) -> None:
        if poll_lock.locked():
            _LOGGER.debug(
                "[QBIT] poll tick skipped — previous pass still running",
            )
            return

        async with poll_lock:
            await _run_poll_pass(now)

    async def _run_poll_pass(now) -> None:
        store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not store:
            return

        base, = _resolve_base(entry)
        if not base:
            return

        queue = store["queue"]
        if not queue:
            return

        base_path = _resolve_base_path(entry)
        movie_path = _resolve_movie_path(entry)

        for torrent_hash, meta in list(queue.items()):
            if not _is_due(meta, now):
                continue

            meta["last_checked_at"] = now

            index = await _fetch_index(session, base, torrent_hash)
            if index is None:
                continue

            try:
                done = await _process_queue_item(
                    session, base, base_path, movie_path, torrent_hash, meta, index,
                )
            except Exception:
                _LOGGER.exception(
                    "[QBIT] queue processing failed hash=%s",
                    torrent_hash,
                )
                continue

            if done:
                queue.pop(torrent_hash, None)
            else:
                _LOGGER.warning(
                    "[QBIT] queue retry hash=%s — one or more steps failed, retrying next tick",
                    torrent_hash,
                )

    unsub = async_track_time_interval(hass, _poll_queue, _POLL_INTERVAL)
    hass.data[DOMAIN][entry.entry_id]["unsub_poll"] = unsub

    cleanup_time = _resolve_cleanup_time(entry)
    if cleanup_time is not None:
        cleanup_hour, cleanup_minute = cleanup_time

        async def _run_scheduled_cleanup(now) -> None:
            base, = _resolve_base(entry)
            if not base:
                return
            temp_ha_path = _resolve_temp_ha_path(entry)
            await _cleanup_temp_folders(hass, session, base, temp_ha_path)

        unsub_cleanup = async_track_time_change(
            hass, _run_scheduled_cleanup,
            hour=cleanup_hour, minute=cleanup_minute, second=0,
        )
        hass.data[DOMAIN][entry.entry_id]["unsub_cleanup"] = unsub_cleanup

    hass.services.async_register(
        DOMAIN,
        "add_magnet",
        add_magnet,
    )

    hass.services.async_register(
        DOMAIN,
        "flush_orphaned",
        flush_orphaned,
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
        "flush_orphaned",
    )

    store = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if store is not None:
        unsub = store.get("unsub_poll")
        if unsub is not None:
            unsub()

        unsub_cleanup = store.get("unsub_cleanup")
        if unsub_cleanup is not None:
            unsub_cleanup()

    return True