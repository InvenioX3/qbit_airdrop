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
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import remux
from .const import (
    DOMAIN,
    CONF_BASE_PATH,
    CONF_DOWNLOAD_PATH,
    CONF_MOVIE_PATH,
    CONF_TEMP_HA_PATH,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_MKVMERGE_PATH,
    TAG_REMUXED,
    TAG_REMUX_SKIPPED,
)
from .store import TorrentStore
from .util import resolve_base as _resolve_base

_LOGGER = logging.getLogger(__name__)

_POLL_INTERVAL = timedelta(seconds=15)
_COMMAND_DELAY = 0.25

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


def _resolve_ssh_host(entry: ConfigEntry) -> str:
    # SSH targets the same machine that hosts qBittorrent (and mkvmerge) —
    # reuse the connection host field, stripped of any scheme/port the user
    # may have included there for the WebUI connection.
    data = entry.options or entry.data or {}
    host = (data.get("host") or "").strip().strip("/")
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.split(":", 1)[0]


def _resolve_ssh_port(entry: ConfigEntry) -> int:
    data = entry.options or entry.data or {}
    try:
        return int(data.get(CONF_SSH_PORT) or 22)
    except (TypeError, ValueError):
        return 22


def _resolve_ssh_username(entry: ConfigEntry) -> str:
    data = entry.options or entry.data or {}
    return (data.get(CONF_SSH_USERNAME) or "").strip()


def _resolve_mkvmerge_path(entry: ConfigEntry) -> str:
    data = entry.options or entry.data or {}
    return (data.get(CONF_MKVMERGE_PATH) or "").strip()


def _cleanup_temp_folders_sync(temp_root: str, existing_names: set[str]) -> dict:
    """Blocking filesystem work — must be run via hass.async_add_executor_job.
    Returns diagnostic counts plus the list of folder paths actually removed.
    A folder is a removal candidate once no torrent — in any state — still
    exists with that exact name; as long as qBittorrent still tracks it at
    all (even just seeding), its folder is left alone. No further content
    check beyond that: once a torrent's gone from qBittorrent entirely,
    whatever's left behind (including leftovers from qBittorrent itself
    occasionally failing to fully clean up on delete) is removed too."""
    result = {
        "scanned": 0,
        "orphaned": 0,
        "removed": [],
    }

    try:
        entries = os.listdir(temp_root)
    except OSError:
        _LOGGER.exception("[QBIT] temp cleanup: could not list %s", temp_root)
        return result

    for name in entries:
        folder_path = os.path.join(temp_root, name)
        if not os.path.isdir(folder_path):
            continue

        result["scanned"] += 1

        if name in existing_names:
            continue  # still tracked by qBittorrent, in any state

        result["orphaned"] += 1

        try:
            shutil.rmtree(folder_path)
            result["removed"].append(folder_path)
        except OSError:
            _LOGGER.exception("[QBIT] temp cleanup: could not remove %s", folder_path)

    return result


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


async def _fetch_index(session, base, torrent_hash: str) -> dict | None:
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
            "priority": entry.get("priority"),
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
    # folder, never the folder itself or qBittorrent's own tracked name. A
    # folder is only a removal candidate once no torrent, in any state,
    # still exists with that name — state doesn't matter, only whether it's
    # still tracked at all.
    existing_names = {str(t.get("name") or "") for t in live}
    _LOGGER.warning(
        "[QBIT] temp cleanup: %d torrents currently tracked",
        len(existing_names),
    )

    # qBittorrent answered with 200 but reported zero torrents — never trust
    # this enough to proceed. A momentary startup race, a transient bug, or
    # any other reason the response undercounts would otherwise make every
    # folder look orphaned and wipe them all. Skip this pass entirely rather
    # than risk it; the next scheduled run (or a manual trigger) tries again.
    if not existing_names:
        _LOGGER.warning(
            "[QBIT] temp cleanup: qBittorrent reported zero torrents — "
            "treating as a suspicious response and skipping this pass "
            "rather than risk mass-deleting folders"
        )
        return

    result = await hass.async_add_executor_job(
        _cleanup_temp_folders_sync, temp_ha_path, existing_names,
    )
    _LOGGER.warning(
        "[QBIT] temp cleanup: %d folder(s) scanned, %d orphaned (no matching "
        "torrent left), %d removed",
        result["scanned"], result["orphaned"], len(result["removed"]),
    )
    for path in result["removed"]:
        _LOGGER.debug("[QBIT] temp cleanup removed %s", path)


def _find_available_name(base_name: str, existing_names: set[str]) -> str:
    if base_name not in existing_names:
        return base_name
    n = 1
    while f"{base_name} ({n})" in existing_names:
        n += 1
    return f"{base_name} ({n})"


async def _collect_other_video_filenames(session, base, torrent_hash, category, torrents: dict) -> set[str]:
    """Video-file basenames (no extension) of other tracked torrents in the
    same internal category (empty string = category-less/movies) — scoped
    off our own persistent record now that qBittorrent's own category field
    is never set.

    The collision unit — for both movies and TV — is the individual video
    file name, never a folder. Same show + same season is always meant to
    merge into one shared season folder; the only genuine TV collision is
    two torrents wanting the same episode at once, which shows up as two
    files wanting the same name.

    Still-queued (pending, pre-metadata) "se" torrents and movies haven't
    been renamed yet, but their eventual filename is already known from
    rename_name at add time — checked directly rather than skipped, closing
    the race where two same-episode (or same-movie) torrents are added
    back-to-back before either reaches metadata. Season-pack types
    ("s"/"season"/"complete") can't be checked this way since their
    individual episode names aren't known until metadata arrives — a
    narrower, accepted gap."""
    names = set()

    for other_hash, rec in torrents.items():
        if other_hash == torrent_hash:
            continue
        if (rec.get("category") or "") != category:
            continue

        if rec.get("stage") == "pending":
            if rec.get("token_type") == "se" or not category:
                nm = rec.get("rename_name")
                if nm:
                    names.add(nm)
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


async def _add_tags(session, base, torrent_hash, tags: str) -> bool:
    return await _qbit_command(
        session, base, "addTags",
        {"hashes": torrent_hash, "tags": tags},
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


async def _process_queue_item(
    session, base, download_path, torrent_hash, meta, index, torrents,
) -> tuple[bool, bool]:
    """Renames per the existing classification pipeline, then relocates via
    setLocation exactly as it always has — rooted at Qbittorrent default
    save path rather than a category-derived path, since qBittorrent's own
    category field is never set (Goal 1). This is NOT the final Movies/TV
    Shows destination — it's where qBittorrent settles permanently once
    metadata resolves; the remux pass reads from there and writes the true
    final output separately. Returns (done, needs_remux); needs_remux is
    False only for an unrecognized token type, where there's nothing for a
    later remux pass to do."""
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
        # Movies land as individual files, so the dedup unit is the file
        # name itself — check other currently-tracked category-less
        # torrents for a collision.
        other_names = await _collect_other_video_filenames(session, base, torrent_hash, "", torrents)
        target_name = _find_available_name(rename_name, other_names)
        ok &= await _rename_single_file_target(
            session, base, torrent_hash, files, largest, root_folder,
            target_name, target_name, force_keep_all=is_bluray,
        )
        if download_path:
            ok &= await _set_location(
                session, base, torrent_hash, _build_location(download_path, target_name),
            )

    elif token_type == "se":
        # Same show + same season always merges into one shared season
        # folder — that's not a collision. The only genuine TV collision is
        # two torrents downloading the same episode at once, which shows up
        # as two files wanting the same name.
        other_names = await _collect_other_video_filenames(session, base, torrent_hash, category, torrents)
        target_name = _find_available_name(rename_name, other_names)
        ok &= await _rename_single_file_target(
            session, base, torrent_hash, files, largest, root_folder,
            season, target_name, force_keep_all=is_bluray,
        )
        if download_path:
            location = (
                _build_location(download_path, category)
                if root_folder else _build_location(download_path, category, season)
            )
            ok &= await _set_location(session, base, torrent_hash, location)

    elif token_type in ("s", "season"):
        # Same show + same season always merges into one shared season
        # folder — that's not a collision. Each episode file is checked
        # individually, since a pack can contain several, and each one
        # could independently collide with a different in-progress
        # duplicate download.
        used_names = await _collect_other_video_filenames(session, base, torrent_hash, category, torrents)

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
            target_name = _find_available_name(f"{category} {episode}", used_names)
            used_names.add(target_name)
            new_path = _sibling_path(f["path"], f"{target_name}{ext}")
            ok &= await _rename_file(session, base, torrent_hash, f["path"], new_path)

        if root_folder:
            ok &= await _rename_folder(session, base, torrent_hash, root_folder, season)

        ok &= await _apply_file_priorities(session, base, torrent_hash, files, keep_ids)
        if download_path:
            ok &= await _set_location(session, base, torrent_hash, _build_location(download_path, category))

    elif token_type == "complete":
        # Same per-file dedup as "s"/"season" — root folder merging into the
        # category folder is intentional here regardless, so only the
        # individual episode files need a collision check.
        used_names = await _collect_other_video_filenames(session, base, torrent_hash, category, torrents)

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
            target_name = _find_available_name(f"{category} {episode}", used_names)
            used_names.add(target_name)
            new_path = _sibling_path(f["path"], f"{target_name}{ext}")
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

        # Root folder was just renamed to `category` itself — the resolved
        # location only needs download_path, or it'd end up download_path/category/category/...
        if download_path:
            location = (
                _build_location(download_path)
                if root_folder else _build_location(download_path, category)
            )
            ok &= await _set_location(session, base, torrent_hash, location)

    else:
        _LOGGER.warning(
            "[QBIT] unrecognized token_type=%s hash=%s — skipping rename pipeline",
            token_type, torrent_hash,
        )
        return True, False

    ok &= await _start_torrent(session, base, torrent_hash)
    return ok, True


_COMPLETE_STATE_SUFFIX = "up"


def _remux_dest_dir(tv_shows_path, movie_path, category, token_type, season, file_rel_path) -> str:
    """Where a given (already-relocated-by-setLocation) file's remuxed
    output should land. TV nests under category[\\season]; "complete" packs
    detect the season from the file's own current parent folder, since
    multi-season torrents nest episodes under normalized season-code
    subfolders rather than a single fixed season."""
    if not category:
        return movie_path

    if token_type == "complete":
        parent_leaf = file_rel_path.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in file_rel_path else ""
        season_seg = _detect_season(parent_leaf)
        return (
            _build_location(tv_shows_path, category, season_seg)
            if season_seg else _build_location(tv_shows_path, category)
        )

    return _build_location(tv_shows_path, category, season)


async def _run_remux_pass(hass, entry, session, base, store) -> None:
    pending_remux = {h: rec for h, rec in store.torrents.items() if rec.get("stage") == "awaiting_remux"}
    if not pending_remux:
        return

    ssh_host = _resolve_ssh_host(entry)
    ssh_username = _resolve_ssh_username(entry)
    mkvmerge_path = _resolve_mkvmerge_path(entry)
    if not (ssh_host and ssh_username and mkvmerge_path):
        _LOGGER.warning(
            "[QBIT] remux pass: SSH not fully configured (host=%r user=%r mkvmerge_path=%r) — "
            "%d torrent(s) waiting in awaiting_remux, none will be processed until all three are set",
            ssh_host, ssh_username, mkvmerge_path, len(pending_remux),
        )
        return

    tv_shows_path = _resolve_base_path(entry)
    movie_path = _resolve_movie_path(entry)

    try:
        async with session.get(
            f"{base}/api/v2/torrents/info",
            params={"filter": "all"},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning("[QBIT] remux pass: torrent list fetch status=%s", resp.status)
                return
            live = await resp.json(content_type=None)
    except Exception:
        _LOGGER.exception("[QBIT] remux pass: torrent list fetch failed")
        return

    if not isinstance(live, list):
        return

    live_by_hash = {str(t.get("hash") or "").lower(): t for t in live}
    ssh_port = _resolve_ssh_port(entry)
    private_key, _public_key = await remux.get_or_create_keypair(hass, entry.entry_id)

    _LOGGER.debug("[QBIT] remux pass: %d torrent(s) currently awaiting remux", len(pending_remux))

    for torrent_hash, rec in pending_remux.items():
        t = live_by_hash.get(torrent_hash)
        if not t:
            _LOGGER.debug("[QBIT] remux pass: hash=%s no longer in qBittorrent, skipping", torrent_hash)
            continue

        state = str(t.get("state") or "").lower()
        if not state.endswith(_COMPLETE_STATE_SUFFIX):
            _LOGGER.debug("[QBIT] remux pass: hash=%s not complete yet (state=%s)", torrent_hash, state)
            continue

        tag_set = {tg.strip() for tg in str(t.get("tags") or "").split(",") if tg.strip()}
        if TAG_REMUXED in tag_set or TAG_REMUX_SKIPPED in tag_set:
            continue

        category = rec.get("category") or ""
        token_type = rec.get("token_type") or ""
        season = rec.get("season") or ""

        if not category and not movie_path:
            _LOGGER.debug("[QBIT] remux pass: hash=%s is a movie but Movies save path isn't configured", torrent_hash)
            continue
        if category and not tv_shows_path:
            _LOGGER.debug("[QBIT] remux pass: hash=%s is TV but TV Shows save path isn't configured", torrent_hash)
            continue

        index = await _fetch_index(session, base, torrent_hash)
        if not index:
            _LOGGER.debug("[QBIT] remux pass: hash=%s file index fetch failed", torrent_hash)
            continue

        save_path = str(t.get("save_path") or "").rstrip("\\/")
        videos = [
            f for f in index["files"]
            if _is_video(f["path"]) and f.get("priority") != 0
        ]
        if not videos:
            _LOGGER.debug("[QBIT] remux pass: hash=%s no eligible (kept) video files found", torrent_hash)
            continue

        _LOGGER.warning(
            "[QBIT] remux pass: starting hash=%s category=%r token_type=%r files=%d save_path=%s",
            torrent_hash, category, token_type, len(videos), save_path,
        )

        all_succeeded = True
        any_skipped = False

        for f in videos:
            file_name = os.path.basename(f["path"])
            source_path = f"{save_path}\\{f['path'].replace('/', chr(92))}"
            dest_dir = _remux_dest_dir(tv_shows_path, movie_path, category, token_type, season, f["path"])
            dest_path = f"{dest_dir.rstrip(chr(92))}\\{file_name}"

            _LOGGER.warning(
                "[QBIT] remux pass: hash=%s attempting file=%s source=%s dest=%s",
                torrent_hash, file_name, source_path, dest_path,
            )

            success, skipped = await remux.remux_file(
                ssh_host, ssh_port, ssh_username, private_key, mkvmerge_path,
                source_path, dest_path,
            )

            if skipped:
                _LOGGER.warning(
                    "[QBIT] remux pass: hash=%s file=%s SKIPPED — undefined-language track present",
                    torrent_hash, file_name,
                )
                any_skipped = True
            elif success:
                _LOGGER.warning(
                    "[QBIT] remux pass: hash=%s file=%s SUCCEEDED -> %s",
                    torrent_hash, file_name, dest_path,
                )
            else:
                _LOGGER.warning(
                    "[QBIT] remux pass: hash=%s file=%s FAILED — will retry next pass",
                    torrent_hash, file_name,
                )
                all_succeeded = False

        if any_skipped:
            await _add_tags(session, base, torrent_hash, TAG_REMUX_SKIPPED)
            _LOGGER.warning("[QBIT] remux pass: hash=%s tagged %r", torrent_hash, TAG_REMUX_SKIPPED)
        elif all_succeeded:
            await _add_tags(session, base, torrent_hash, TAG_REMUXED)
            _LOGGER.warning("[QBIT] remux pass: hash=%s all files remuxed, tagged %r", torrent_hash, TAG_REMUXED)
        else:
            _LOGGER.warning(
                "[QBIT] remux pass: hash=%s had at least one failure — leaving untagged, will retry next pass",
                torrent_hash,
            )


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
        QbitAirdropSshKeyView,
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

    hass.http.register_view(
        QbitAirdropSshKeyView(
            hass,
            entry,
        )
    )

    session = aiohttp_client.async_get_clientsession(hass)
    poll_lock = asyncio.Lock()

    store = TorrentStore(hass, entry.entry_id)
    await store.async_load()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "store": store,
    }

    # Generate the SSH keypair up front (idempotent — a no-op once one
    # exists) so the public key is available via QbitAirdropSshKeyView as
    # soon as the integration loads, rather than only after the first remux
    # attempt.
    await remux.get_or_create_keypair(hass, entry.entry_id)

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

        # category is intentionally never sent to qBittorrent — it never
        # drove file placement (every move was always an explicit API call),
        # so it's kept only as our own internal classification field.

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

        store.torrents[torrent_hash] = {
            "category": category,
            "rename_name": (data.get("rename_name") or "").strip(),
            "token_type": (data.get("token_type") or "").strip(),
            "season": (data.get("season") or "").strip(),
            "stage": "pending",
            "added_at": dt_util.utcnow(),
            "last_checked_at": None,
        }
        await store.async_save()

    async def flush_orphaned(call: ServiceCall) -> None:
        if not store.torrents:
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

        changed = False
        for torrent_hash in list(store.torrents):
            if torrent_hash not in live_hashes:
                store.torrents.pop(torrent_hash, None)
                changed = True
                _LOGGER.debug("[QBIT] flush_orphaned removed hash=%s", torrent_hash)

        if changed:
            await store.async_save()

    async def run_cleanup(call: ServiceCall) -> None:
        base, = _resolve_base(entry)
        if not base:
            return
        temp_ha_path = _resolve_temp_ha_path(entry)
        await _cleanup_temp_folders(hass, session, base, temp_ha_path)

    async def _poll_queue(now) -> None:
        if poll_lock.locked():
            _LOGGER.debug(
                "[QBIT] poll tick skipped — previous pass still running",
            )
            return

        async with poll_lock:
            await _run_poll_pass(now)

    async def _run_poll_pass(now) -> None:
        base, = _resolve_base(entry)
        if not base:
            return

        download_path = _resolve_download_path(entry)

        pending = {h: rec for h, rec in store.torrents.items() if rec.get("stage") == "pending"}
        changed = False

        for torrent_hash, meta in pending.items():
            if not _is_due(meta, now):
                continue

            meta["last_checked_at"] = now
            changed = True

            index = await _fetch_index(session, base, torrent_hash)
            if index is None:
                continue

            try:
                done, needs_remux = await _process_queue_item(
                    session, base, download_path, torrent_hash, meta, index,
                    store.torrents,
                )
            except Exception:
                _LOGGER.exception(
                    "[QBIT] queue processing failed hash=%s",
                    torrent_hash,
                )
                continue

            if done:
                if needs_remux:
                    meta["stage"] = "awaiting_remux"
                else:
                    store.torrents.pop(torrent_hash, None)
            else:
                _LOGGER.warning(
                    "[QBIT] queue retry hash=%s — one or more steps failed, retrying next tick",
                    torrent_hash,
                )

        if changed:
            await store.async_save()

        await _run_remux_pass(hass, entry, session, base, store)

    unsub = async_track_time_interval(hass, _poll_queue, _POLL_INTERVAL)
    hass.data[DOMAIN][entry.entry_id]["unsub_poll"] = unsub

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

    hass.services.async_register(
        DOMAIN,
        "run_cleanup",
        run_cleanup,
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

    hass.services.async_remove(
        DOMAIN,
        "run_cleanup",
    )

    store = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if store is not None:
        unsub = store.get("unsub_poll")
        if unsub is not None:
            unsub()

    return True
