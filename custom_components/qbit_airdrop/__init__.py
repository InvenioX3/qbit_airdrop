from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse, parse_qs
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

    m = re.search(r"\b(S\d{1,2})(?:-\d{1,2})?\b", dn, re.I)
    if m:
        return m.group(1).upper()

    return ""
    
def _clean_title(name_raw: str) -> str:
    name = os.path.splitext(
        str(name_raw or "")
    )[0]

    if not name:
        return ""

    se = re.search(
        r"\bS\d{1,2}E\d{1,3}\b",
        name,
        re.I,
    )

    s = re.search(
        r"\bS\d{1,2}\b(?!-\d)",
        name,
        re.I,
    )

    season = re.search(
        r"\bSeason\s+\d+(?:\s*-\s*\d+)?\b",
        name,
        re.I,
    )

    complete = re.search(
        r"\b(?:Complete\s+Series|Complete\s+Season)\b",
        name,
        re.I,
    )

    yr = re.search(
        r"\(?\b(?:19|20)\d{2}\b\)?",
        name,
        re.I,
    )

    token = None
    token_type = None

    if se:
        token = se
        token_type = "se"
    elif s:
        token = s
        token_type = "s"
    elif season:
        token = season
        token_type = "season"
    elif complete:
        token = complete
        token_type = "complete"
    elif yr:
        token = yr
        token_type = "year"

    normalized = (
        name
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
    )

    if token_type == "year":

        m = re.search(
            r"\(?\b(?:19|20)\d{2}\b\)?",
            normalized,
        )

        if m:
            return (
                normalized[:m.end()]
                .replace("(", "")
                .replace(")", "")
                .replace(".", " ")
                .strip()
            )

    cut = len(name)

    if token:
        cut = token.end()

    kept = normalized[:cut]

    trimmed = re.sub(
        r"[ ._-]+$",
        "",
        kept,
    )

    trimmed = trimmed.replace(
        "(",
        "",
    ).replace(
        ")",
        "",
    )

    if token_type in (
        "se",
        "s",
        "season",
        "complete",
    ):
        trimmed = re.sub(
            r"\b(?:19|20)\d{2}(?=\s+(?:S\d{1,2}(?:E\d{1,3})?|Season\b))",
            "",
            trimmed,
            flags=re.I,
        )

    trimmed = re.sub(
        r'[<>:"/\\|?*]',
        "",
        trimmed,
    )

    return re.sub(
        r"\s+",
        " ",
        trimmed.replace(".", " "),
    ).strip()
    
def _analyze_title(name_raw: str):

    name = os.path.splitext(
        str(name_raw or "")
    )[0]

    if not name:
        return {
            "token_type": None,
        }

    se = re.search(
        r"\bS\d{1,2}E\d{1,3}\b",
        name,
        re.I,
    )

    s = re.search(
        r"\bS\d{1,2}\b(?!-\d)",
        name,
        re.I,
    )

    season = re.search(
        r"\bSeason\s+\d+(?:\s*-\s*\d+)?\b",
        name,
        re.I,
    )

    complete = re.search(
        r"\b(?:Complete\s+Series|Complete\s+Season)\b",
        name,
        re.I,
    )

    yr = re.search(
        r"\(?\b(?:19|20)\d{2}\b\)?",
        name,
        re.I,
    )

    if se:
        return {"token_type": "se"}

    if s:
        return {"token_type": "s"}

    if season:
        return {"token_type": "season"}

    if complete:
        return {"token_type": "complete"}

    if yr:
        return {"token_type": "year"}

    return {
        "token_type": None,
    }
    
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
        
    def classify_files(
        files,
        clean_title,
        season,
    ):
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

        is_episode = bool(
            re.search(
                r"\bS\d{1,2}E\d{1,3}\b",
                clean_title,
                re.I,
            )
        )

        is_movie = (
            not is_episode
            and not season
        )

        records = []

        for f in files:
            path = str(f.get("name", ""))

            filename = os.path.basename(path)

            ext = os.path.splitext(filename)[1].lower()

            record = {
                "id": f.get("index"),
                "path": path,
                "filename": filename,
                "video": ext in video_exts,

                "episode_token": bool(
                    re.search(
                        r"\bS\d{1,2}E\d{1,3}\b",
                        filename,
                        re.I,
                    )
                ),

                "cleaned": _clean_title(
                    filename
                ),

                "matches_clean_title": (
                    _clean_title(filename)
                    == clean_title
                ),

                "keep_candidate": False,
            }

            if is_movie:
                record["keep_candidate"] = (
                    record["video"]
                    and record["matches_clean_title"]
                )

            else:
                record["keep_candidate"] = (
                    record["video"]
                    and record["episode_token"]
                )

            _LOGGER.warning(
                "[QBIT] keep=%s | video=%s | match=%s | clean='%s' | file=%s",
                record["keep_candidate"],
                record["video"],
                record["matches_clean_title"],
                record["cleaned"],
                filename,
            )

            records.append(record)

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
                root_folder = folder_source.split("/", 1)[0]

                folder_name = folder_new

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
                            root_folder,
                            folder_name,
                            body,
                        )

                        return False

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

        except Exception:
            return True

    async def process_pending_queue() -> None:
        while True:

            for torrent_hash, item in list(
                pending_renames.items()
            ):
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

                    continue

                if (
                    item["metadata_ready"]
                    and not item["classified"]
                ):
                    
                    _LOGGER.warning(
                        "[QBIT] stage=classify hash=%s",
                        torrent_hash,
                    )
                    
                    _LOGGER.warning(
                        "[QBIT] classify_input "
                        "hash=%s "
                        "clean_title='%s' "
                        "season='%s'",
                        torrent_hash,
                        item["clean_title"],
                        item["season"],
                    )
                    
                    item["token_type"] = (
                        item["token_type"]
                    )

                    item["keep_files"] = [
                        r for r in classify_files(
                            item["files"],
                            item["clean_title"],
                            item["season"],
                        )
                        if r["keep_candidate"]
                    ]

                    if not item["keep_files"]:

                        _LOGGER.warning(
                            "[QBIT] no_keep_files hash=%s",
                            torrent_hash,
                        )

                        pending_renames.pop(
                            torrent_hash,
                            None,
                        )

                        continue

                    _LOGGER.warning(
                        "[QBIT] classify result hash=%s keep=%s",
                        torrent_hash,
                        len(item["keep_files"]),
                    )

                    if item["keep_files"]:

                        keep = item["keep_files"][0]

                        if "/" in keep["path"]:

                            item["folder_old"] = (
                                keep["path"]
                                .split("/", 1)[0]
                            )

                        if item["folder_old"]:

                            if item["token_type"] in (
                                "season",
                                "complete",
                            ):

                                item["folder_new"] = (
                                    item["category"]
                                )

                            elif item["token_type"] == "se":

                                item["folder_new"] = (
                                    item["season"]
                                )

                            else:

                                item["folder_new"] = (
                                    item["rename_name"]
                                )

                        else:

                            item["folder_new"] = ""

                        if len(item["keep_files"]) == 1:

                            item["file_old"] = keep["path"]

                            ext = os.path.splitext(
                                keep["filename"]
                            )[1]

                            item["file_ext"] = ext

                        _LOGGER.warning(
                            "[QBIT] targets hash=%s "
                            "folder_old='%s' "
                            "folder_new='%s' "
                            "file_old='%s' "
                            "file_new='%s'",
                            torrent_hash,
                            item["folder_old"],
                            item["folder_new"],
                            item["file_old"],
                            item["file_new"],
                        )

                    item["classified"] = True

                    continue

                #
                # folder_request
                #
                if (
                    item["folder_old"]
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

                        if (
                            item["folder_old"]
                            and item["file_old"]
                        ):
                            item["file_old"] = (
                                item["file_old"]
                                .replace(
                                    item["folder_old"],
                                    item["folder_new"],
                                    1,
                                )
                            )

                        if (
                            item["folder_old"]
                            and item["file_new"]
                        ):
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

                if (
                    len(item["keep_files"]) == 1
                    and not item["file_new"]
                ):

                    keep = item["keep_files"][0]

                    if item["folder_old"]:

                        item["file_new"] = (
                            f"{item['folder_new']}/"
                            f"{item['rename_name']}"
                            f"{item['file_ext']}"
                        )

                    else:

                        item["file_new"] = (
                            f"{item['rename_name']}"
                            f"{item['file_ext']}"
                        )

                    item["folder_verified"] = True

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

                    continue

                #
                # complete
                #
                _LOGGER.warning(
                    "[QBIT] stage=complete hash=%s",
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
        magnet = (data.get("magnet") or "").strip()
        category = (data.get("category") or "").strip()
        clean_title = (data.get("clean_title") or "").strip()
        
        token_type = (
            data.get("token_type")
            or None
        )

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

        # Movies get metadata tags
        if not season and media_parts:
            rename_name = (
                f"{clean_title} "
                f"[{' • '.join(media_parts)}]"
            )

        savepath = ""
        if category and base_path:
            # Join base_path + category, ensure trailing slash
            sep_needed = not (base_path.endswith("/") or base_path.endswith("\\"))
            savepath = f"{base_path}{'/' if sep_needed else ''}{category}"
            
            # Single episode torrents without folders
            #
            if (
                season
                and category
                and clean_title != category
            ):
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
                
                if torrent_hash:

                    try:

                        await session.post(
                            f"{base}/api/v2/torrents/setStopCondition",
                            data={
                                "hashes": torrent_hash,
                                "stopCondition": "MetadataReceived",
                            },
                            timeout=10,
                        )

                    except Exception:
                        pass

                if torrent_hash and (
                    rename_name or
                    season
                ):
                    
                    _LOGGER.warning(
                        "[QBIT] queue_create hash=%s",
                        torrent_hash,
                    )
                    
                    pending_renames[torrent_hash] = {
                        "base": base,
                        "rename_name": rename_name,
                        "season": season,
                        "clean_title": clean_title,
                        "category": category,
                        "token_type": token_type,

                        "metadata_ready": False,
                        "classified": False,
                        
                        "folder_requested": False,
                        "folder_verified": False,

                        "file_requested": False,

                        "files": [],
                        "keep_files": [],

                        "folder_old": "",
                        "folder_new": "",

                        "file_old": "",
                        "file_new": "",
                        "file_ext": "",
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
