from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse
import asyncio
import os
import re
import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import aiohttp_client

_LOGGER = logging.getLogger(__name__)

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
    
    try:
        query = magnet.split("?", 1)[1]
        dn = parse_qs(query).get("dn", [""])[0]
    except Exception:
        return ""

    dn = dn.replace("+", " ")

    m = re.search(r"\b(S\d{1,2})E\d{1,3}\b", dn, re.I)
    if m:
        return m.group(1).upper()

    m = re.search(r"\b(S\d{1,2})(?:-\d{1,2})?\b", dn, re.I)
    if m:
        return m.group(1).upper()

    return ""

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
    
    async def enumerate_files(
        base: str,
        torrent_hash: str,
    ):
        try:
            files = []

            for _ in range(60):
                async with session.get(
                    f"{base}/api/v2/torrents/files",
                    params={"hash": torrent_hash},
                    timeout=10,
                ) as resp:

                    files = await resp.json()

                if files:
                    return files

                await asyncio.sleep(1)

            return []

        except Exception as e:
            _LOGGER.exception(
                "[QBIT] enumerate_files failed: %s",
                e,
            )
            return []
        
    def enumerate_files_metadata(files):
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

        records = []

        for f in files:
            path = str(f.get("name", ""))
            filename = os.path.basename(path)

            records.append(
                {
                    "id": f.get("index"),
                    "path": path,
                    "filename": filename,
                    "size": int(
                        f.get("size", 0)
                        or 0
                    ),
                    "video": (
                        os.path.splitext(filename)[1].lower()
                        in video_exts
                    ),
                }
            )

        return records


    async def process_torrent(
        base: str,
        torrent_hash: str,
        keep_files,
        folder_old: str,
        folder_new: str,
    ) -> bool:

        try:

            if not keep_files:
                return False
            
            folder_source = None

            if keep_files:
                folder_source = keep_files[0]["path"]

            if (
                folder_source
                and "/" in folder_source
            ):

                async with session.post(
                    f"{base}/api/v2/torrents/renameFolder",
                    data={
                        "hash": torrent_hash,
                        "oldPath": folder_old,
                        "newPath": folder_new,
                    },
                    timeout=10,
                ) as resp:

                    if resp.status >= 400:
                        body = await resp.text()

                        _LOGGER.warning(
                            "[QBIT] renameFolder failed | status=%s | old=%s | new=%s | body=%s",
                            resp.status,
                            folder_old,
                            folder_new,
                            body,
                        )

                        return False
                        
                return True

        except Exception as e:
            _LOGGER.exception(
                "[QBIT] process_torrent failed: %s",
                e,
            )
            return False

        return True
        
    async def rename_file(
        base,
        torrent_hash,
        old_path,
        new_path,
    ):
        try:

            _LOGGER.warning(
                "[QBIT] rename_file hash=%s old='%s' new='%s'",
                torrent_hash,
                old_path,
                new_path,
            )

            async with session.post(
                f"{base}/api/v2/torrents/renameFile",
                data={
                    "hash": torrent_hash,
                    "oldPath": old_path,
                    "newPath": new_path,
                },
                timeout=10,
            ) as resp:

                if resp.status >= 400:

                    body = await resp.text()

                    _LOGGER.warning(
                        "[QBIT] renameFile failed "
                        "| status=%s "
                        "| old=%s "
                        "| new=%s "
                        "| body=%s",
                        resp.status,
                        old_path,
                        new_path,
                        body,
                    )

                    return False

            return True

        except Exception as e:

            _LOGGER.exception(
                "[QBIT] rename_file failed: %s",
                e,
            )

            return False
            
    async def set_file_priorities(
        base,
        torrent_hash,
        file_ids,
        priority,
    ):
        try:

            if not file_ids:
                return True

            async with session.post(
                f"{base}/api/v2/torrents/filePrio",
                data={
                    "hash": torrent_hash,
                    "id": "|".join(
                        str(i)
                        for i in file_ids
                    ),
                    "priority": priority,
                },
                timeout=10,
            ) as resp:

                return resp.status < 400

        except Exception as e:

            _LOGGER.exception(
                "[QBIT] filePrio failed: %s",
                e,
            )

            return False

    async def resume_torrent(
        base,
        torrent_hash,
    ):
        try:

            async with session.post(
                f"{base}/api/v2/torrents/resume",
                data={
                    "hashes": torrent_hash,
                },
                timeout=10,
            ) as resp:

                return resp.status < 400

        except Exception as e:

            _LOGGER.exception(
                "[QBIT] resume failed: %s",
                e,
            )

            return False

    async def torrent_exists(
        base: str,
        torrent_hash: str,
    ) -> bool:
        try:
            async with session.get(
                f"{base}/api/v2/torrents/info",
                params={"hashes": torrent_hash},
                timeout=10,
            ) as resp:
                data = await resp.json()

            return bool(data)

        except Exception as e:

            _LOGGER.exception(
                "[QBIT] torrent_exists failed: %s",
                e,
            )

            return True
            
    async def process_pending_queue() -> None:
        while True:

            for torrent_hash, item in list(
                pending_renames.items()
            ):
                
                _LOGGER.warning(
                    "[QBIT] queue_tick hash=%s",
                    torrent_hash,
                )
                
                exists = await torrent_exists(
                    item["base"],
                    torrent_hash,
                )

                if not exists:
                    pending_renames.pop(
                        torrent_hash,
                        None,
                    )
                    continue

                if not item["metadata_ready"]:
                    
                    _LOGGER.warning(
                        "[QBIT] stage=metadata hash=%s",
                        torrent_hash,
                    )

                    files = await enumerate_files(
                        item["base"],
                        torrent_hash,
                    )

                    if files:
                        item["files"] = files
                        item["metadata_ready"] = True
                        
                        _LOGGER.warning(
                            "[QBIT] metadata_received hash=%s files=%s",
                            torrent_hash,
                            len(files),
                        )

                video_files = [
                    f
                    for f in enumerate_files_metadata(
                        item["files"]
                    )
                    if f["video"]
                ]

                #
                # determine_keep_files
                #

                if (
                    item["metadata_ready"]
                    and not item["keep_files"]
                ):

                    candidates = []

                    title = (
                        item["clean_title"]
                        .lower()
                    )

                    matching = []

                    for f in video_files:

                        name = (
                            f["filename"]
                            .lower()
                        )

                        if title in name:
                            matching.append(f)

                    if item["token_type"] in (
                        "season",
                        "complete",
                    ):

                        candidates = matching

                    else:

                        candidates = (
                            [max(
                                matching,
                                key=lambda x: x["size"],
                            )]
                            if matching
                            else []
                        )

                    item["keep_files"] = candidates
                    
                    _LOGGER.warning(
                        "[QBIT] candidates=%s title='%s'",
                        [f["filename"] for f in candidates],
                        title,
                    )

                    keep_ids = {
                        f["id"]
                        for f in candidates
                    }

                    item["drop_files"] = [
                        f
                        for f in enumerate_files_metadata(
                            item["files"]
                        )
                        if f["id"] not in keep_ids
                    ]

                    if candidates:

                        keep = candidates[0]

                        if "/" in keep["path"]:

                            item["folder_old"] = (
                                keep["path"]
                                .split("/", 1)[0]
                            )

                            if item["token_type"] == "year":

                                item["folder_new"] = (
                                    item["clean_title"]
                                )

                            elif item["token_type"] == "se":

                                item["folder_new"] = (
                                    item["season"]
                                )

                            else:

                                item["folder_new"] = (
                                    item["category"]
                                )

                        if len(candidates) == 1:

                            item["file_old"] = keep["path"]

                            ext = os.path.splitext(
                                keep["filename"]
                            )[1]

                            if "/" in keep["path"]:

                                current_folder = (
                                    keep["path"]
                                    .rsplit("/", 1)[0]
                                )

                                item["file_new"] = (
                                    f"{current_folder}/"
                                    f"{item['rename_name']}"
                                    f"{ext}"
                                )

                            else:

                                item["file_new"] = (
                                    f"{item['rename_name']}"
                                    f"{ext}"
                                )

                    _LOGGER.warning(
                        "[QBIT] keep_files=%s drop_files=%s",
                        len(item["keep_files"]),
                        len(item["drop_files"]),
                    )

                #
                # priorities_request
                #
                if (
                    item["metadata_ready"]
                    and not item["priorities_requested"]
                ):

                    _LOGGER.warning(
                        "[QBIT] stage=priorities_request hash=%s",
                        torrent_hash,
                    )

                    drop_ids = [
                        f["id"]
                        for f in item["drop_files"]
                    ]

                    ok = await set_file_priorities(
                        item["base"],
                        torrent_hash,
                        drop_ids,
                        0,
                    )

                    _LOGGER.warning(
                        "[QBIT] filePrio ok=%s drop_ids=%s",
                        ok,
                        drop_ids,
                    )

                    if ok:
                        item["priorities_requested"] = True

                    continue

                #
                # priorities_verify
                #
                if (
                    item["priorities_requested"]
                    and not item["priorities_verified"]
                ):

                    _LOGGER.warning(
                        "[QBIT] stage=priorities_verify hash=%s",
                        torrent_hash,
                    )

                    item["priorities_verified"] = True

                    continue

                #
                # folder_request
                #
                if (
                    item["priorities_verified"]
                    and item["folder_old"]
                    and not item["folder_requested"]
                ):

                    _LOGGER.warning(
                        "[QBIT] stage=folder_request hash=%s",
                        torrent_hash,
                    )

                    ok = await process_torrent(
                        item["base"],
                        torrent_hash,
                        item["keep_files"],
                        item["folder_old"],
                        item["folder_new"],
                    )

                    if ok:

                        if item["folder_old"]:

                            item["file_old"] = (
                                item["file_old"]
                                .replace(
                                    item["folder_old"],
                                    item["folder_new"],
                                    1,
                                )
                            )

                            item["file_new"] = (
                                item["file_new"]
                                .replace(
                                    item["folder_old"],
                                    item["folder_new"],
                                    1,
                                )
                            )

                        item["folder_requested"] = True

                    continue

                #
                # folder_verify
                #
                if (
                    item["folder_old"]
                    and item["folder_requested"]
                    and not item["folder_verified"]
                ):

                    _LOGGER.warning(
                        "[QBIT] stage=folder_verify hash=%s",
                        torrent_hash,
                    )

                    files = await enumerate_files(
                        item["base"],
                        torrent_hash,
                    )

                    if files:

                        if item["folder_old"]:
                            pass

                        item["folder_verified"] = True

                        _LOGGER.warning(
                            "[QBIT] folder_verified "
                            "hash=%s "
                            "file_old='%s' "
                            "file_new='%s'",
                            torrent_hash,
                            item["file_old"],
                            item["file_new"],
                        )

                    continue

                #
                # file_request
                #
                if (
                    len(item["keep_files"]) == 1
                    and not item["file_requested"]
                ):

                    _LOGGER.warning(
                        "[QBIT] stage=file_request hash=%s",
                        torrent_hash,
                    )

                    ok = await rename_file(
                        item["base"],
                        torrent_hash,
                        item["file_old"],
                        item["file_new"],
                    )

                    if ok:
                        item["file_requested"] = True
                        item["file_verified"] = True

                    continue

                #
                # complete
                #
                _LOGGER.warning(
                    "[QBIT] stage=resume hash=%s",
                    torrent_hash,
                )

                await resume_torrent(
                    item["base"],
                    torrent_hash,
                )

                pending_renames.pop(
                    torrent_hash,
                    None,
                )

            await asyncio.sleep(1)
            
    hass.async_create_task(
        process_pending_queue()
    )

    async def add_magnet(call: ServiceCall) -> None:
        
        data = call.data or {}
        
        _LOGGER.warning(
            "[QBIT] service_data=%s",
            dict(data),
        )
        
        magnet = (data.get("magnet") or "").strip()
        category = (data.get("category") or "").strip()
        clean_title = (data.get("clean_title") or "").strip()
        
        rename_name = (
            data.get("rename_name")
            or ""
        ).strip()

        res = (data.get("res") or "").strip()
        codec = (data.get("codec") or "").strip()
        audio = (data.get("audio") or "").strip()
        if not magnet:
            return

        (base,) = _resolve_base(entry)
        if not base:
            return

        base_path = _resolve_base_path(entry)

        torrent_hash = _hash_from_magnet(magnet)
        
        _LOGGER.warning(
            "[QBIT] hash=%s",
            torrent_hash,
        )

        rename_name = re.sub(
            r'[<>:"/\\|?*]',
            "",
            rename_name,
        )

        savepath = ""
        if category and base_path:
            # Join base_path + category, ensure trailing slash
            sep_needed = not (base_path.endswith("/") or base_path.endswith("\\"))
            savepath = f"{base_path}{'/' if sep_needed else ''}{category}"
            
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
            except Exception as e:

                _LOGGER.exception(
                    "[QBIT] createCategory failed: %s",
                    e,
                )

        # Add magnet (include category/savepath if we have them)
        form = {
            "urls": magnet,
        }

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

                await resp.text()
                
                for _ in range(50):

                    if await torrent_exists(
                        base,
                        torrent_hash,
                    ):
                        break

                    await asyncio.sleep(0.2)
                    
                    _LOGGER.warning(
                        "[QBIT] queue_gate hash=%s rename_name='%s'",
                        torrent_hash,
                        rename_name,
                    )

                _LOGGER.warning(
                    "[QBIT] queue_check hash=%s rename_name='%s'",
                    torrent_hash,
                    rename_name,
                )

                if torrent_hash and rename_name:

                    _LOGGER.warning(
                        "[QBIT] queue_create hash=%s",
                        torrent_hash,
                    )
                    
                    pending_renames[torrent_hash] = {
                        "base": base,

                        "rename_name": rename_name,
                        "clean_title": clean_title,
                        "category": category,
                        
                        "token_type": (
                            data.get("token_type")
                            or ""
                        ),

                        "season": (
                            data.get("season")
                            or ""
                        ),
                        
                        "metadata_ready": False,
                        "priorities_requested": False,
                        "priorities_verified": False,
                        "folder_requested": False,
                        "folder_verified": False,
                        "file_requested": False,
                        "file_verified": False,
                        "files": [],
                        "keep_files": [],
                        "drop_files": [],
                        "folder_old": "",
                        "folder_new": "",
                        "file_old": "",
                        "file_new": "",
                    }
                    
                    _LOGGER.warning(
                        "[QBIT] queue_added hash=%s",
                        torrent_hash,
                    )
                                        
        except Exception as e:

            _LOGGER.exception(
                "[QBIT] add_magnet failed: %s",
                e,
            )

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
