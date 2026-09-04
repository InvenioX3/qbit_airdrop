from __future__ import annotations

import json
import logging
import time
from typing import List

from aiohttp import ClientError, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import remux
from .const import DOMAIN
from .util import is_bluray_structure, resolve_base as _resolve_base

_LOGGER = logging.getLogger(__name__)


class QbitAirdropActiveView(HomeAssistantView):
    url = "/api/qbit_airdrop/active"
    name = "qbit_airdrop:active"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

    async def get(self, request) -> web.Response:
        (base,) = _resolve_base(self.entry)
        if not base:
            return web.json_response({"ok": False, "error": "qB base not configured"}, status=400)

        data_store = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id) or {}
        torrent_store = data_store.get("store")
        queue_hashes = (
            {h for h, rec in torrent_store.torrents.items() if rec.get("stage") == "pending"}
            if torrent_store else set()
        )

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(f"{base}/api/v2/torrents/info?filter=all", timeout=10) as resp:
                body = await resp.text()
                if resp.status != 200:
                    _LOGGER.error("qB fetch failed: %s %s", resp.status, body[:200])
                    return web.json_response({"ok": False, "error": "Fetch failed"}, status=resp.status)
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = json.loads(body)
        except ClientError as err:
            _LOGGER.error("qB request error: %s", err)
            return web.json_response({"ok": False, "error": "Request error"}, status=502)

        # Auto force-start torrents stalled (stalledDL specifically — not
        # stoppedDL, which is a deliberate pause on metadata and must not be
        # touched) for 5+ minutes. Stateless: piggybacks on this endpoint's
        # existing poll cadence and qBittorrent's own last_activity field
        # rather than tracking anything ourselves.
        if isinstance(payload, list):
            now = int(time.time())
            stalled_hashes = [
                str(obj.get("hash") or "").lower()
                for obj in payload
                if str(obj.get("state") or "").lower() == "stalleddl"
                and (now - int(obj.get("last_activity") or now)) >= 300
            ]
            if stalled_hashes:
                try:
                    async with session.post(
                        f"{base}/api/v2/torrents/setForceStart",
                        data={"hashes": "|".join(stalled_hashes), "value": "true"},
                        timeout=10,
                    ):
                        pass
                except ClientError:
                    _LOGGER.exception("[QBIT] auto force-start request error")

        items: List[dict] = []
        if isinstance(payload, list):
            for obj in payload:
                name = str(obj.get("name") or "").strip()
                prog = obj.get("progress", None)
                try:
                    # Truncate rather than round — qBittorrent sets progress to
                    # exactly 1.0 on completion, so this only shows 100% when
                    # actually done instead of prematurely rounding up (e.g.
                    # 99.9% would otherwise display as a misleading 100%).
                    pct = int(float(prog) * 100) if prog is not None else None
                except Exception:
                    pct = None
                if pct is not None:
                    pct = max(0, min(100, pct))

                thash = str(obj.get("hash") or "").lower()
                items.append({
                    "title": name,
                    "percent": pct,
                    "hash": thash,
                    "state": str(obj.get("state") or "").lower(),
                    "size": obj.get("size", None),
                    "in_queue": thash in queue_hashes,
                    # pass-through for the card
                    "dlspeed": obj.get("dlspeed", 0),           # bytes/sec
                    "upspeed": obj.get("upspeed", 0),           # bytes/sec
                    "amount_left": obj.get("amount_left", 0),   # bytes
                    "availability": obj.get("availability", None),
                    # seed information
                    "num_seeds": obj.get("num_seeds", 0),
                    "num_complete": obj.get("num_complete", 0),
                    "added_on": obj.get("added_on", 0),
                    "tags": str(obj.get("tags") or ""),
                })

        # read confirm_delete flag from entry data/options
        d = self.entry.options or self.entry.data or {}
        confirm_delete = bool(d.get("confirm_delete", False))

        return web.json_response({"ok": True, "items": items, "confirm_delete": confirm_delete})

class QbitAirdropDeleteView(HomeAssistantView):
    url = "/api/qbit_airdrop/delete"
    name = "qbit_airdrop:delete"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

    async def post(self, request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        thash = (data.get("hash") or "").strip().lower()
        if not thash:
            return web.json_response({"ok": False, "error": "hash required"}, status=400)

        delete_files = bool(data.get("deleteFiles", True))
        (base,) = _resolve_base(self.entry)
        if not base:
            return web.json_response({"ok": False, "error": "qB base not configured"}, status=400)

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{base}/api/v2/torrents/delete",
                data={"hashes": thash, "deleteFiles": "true" if delete_files else "false"},
                timeout=15,
            ) as resp:
                txt = await resp.text()
                if resp.status != 200:
                    _LOGGER.error("Delete failed: %s %s", resp.status, txt[:200])
                    return web.json_response({"ok": False, "error": "Delete failed"}, status=resp.status)
        except ClientError as err:
            _LOGGER.error("qB POST error: %s", err)
            return web.json_response({"ok": False, "error": "Request error"}, status=502)

        data_store = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        torrent_store = data_store.get("store") if data_store else None
        if torrent_store is not None:
            removed = torrent_store.torrents.pop(thash, None)
            if removed is not None:
                await torrent_store.async_save()
                _LOGGER.debug(
                    "[QBIT] removed hash=%s from tracking after delete",
                    thash,
                )

        return web.json_response({"ok": True})


class QbitAirdropStatsView(HomeAssistantView):
    url = "/api/qbit_airdrop/stats"
    name = "qbit_airdrop:stats"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

    async def get(self, request) -> web.Response:
        (base,) = _resolve_base(self.entry)
        if not base:
            return web.json_response({"ok": False, "error": "qB base not configured"}, status=400)

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(f"{base}/api/v2/transfer/info", timeout=10) as resp:
                if resp.status != 200:
                    return web.json_response({"ok": False, "error": "Fetch failed"}, status=resp.status)
                transfer = await resp.json(content_type=None)

            async with session.get(
                f"{base}/api/v2/sync/maindata",
                params={"rid": 0},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return web.json_response({"ok": False, "error": "Fetch failed"}, status=resp.status)
                maindata = await resp.json(content_type=None)
        except ClientError as err:
            _LOGGER.error("qB request error: %s", err)
            return web.json_response({"ok": False, "error": "Request error"}, status=502)

        dl_speed = transfer.get("dl_info_speed") if isinstance(transfer, dict) else None
        server_state = maindata.get("server_state") if isinstance(maindata, dict) else None
        free_space = server_state.get("free_space_on_disk") if isinstance(server_state, dict) else None
        external_ip = server_state.get("last_external_address_v4") if isinstance(server_state, dict) else None

        return web.json_response({
            "ok": True,
            "dl_speed": dl_speed,
            "free_space": free_space,
            "external_ip": external_ip,
        })


class QbitAirdropSshKeyView(HomeAssistantView):
    """Returns the public half of the auto-generated SSH keypair used to
    reach the mkvmerge host — add it to that host's authorized_keys once.
    The private key never leaves HA's own storage."""
    url = "/api/qbit_airdrop/ssh_pubkey"
    name = "qbit_airdrop:ssh_pubkey"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

    async def get(self, request) -> web.Response:
        _private_key, public_key = await remux.get_or_create_keypair(self.hass, self.entry.entry_id)
        return web.json_response({"ok": True, "public_key": public_key})


class QbitAirdropFilesView(HomeAssistantView):
    """Flat list of a torrent's actual file names — no directory/subfolder
    structure — for the card's file-list overlay."""
    url = "/api/qbit_airdrop/files"
    name = "qbit_airdrop:files"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

    async def get(self, request) -> web.Response:
        thash = (request.query.get("hash") or "").strip().lower()
        if not thash:
            return web.json_response({"ok": False, "error": "hash required"}, status=400)

        (base,) = _resolve_base(self.entry)
        if not base:
            return web.json_response({"ok": False, "error": "qB base not configured"}, status=400)

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{base}/api/v2/torrents/files",
                params={"hash": thash},
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    return web.json_response({"ok": False, "error": "Fetch failed"}, status=resp.status)
                files_raw = await resp.json(content_type=None)
        except ClientError as err:
            _LOGGER.error("qB request error: %s", err)
            return web.json_response({"ok": False, "error": "Request error"}, status=502)

        names = []
        folders = set()
        if isinstance(files_raw, list):
            for entry_obj in files_raw:
                path = str(entry_obj.get("name") or "")
                if not path:
                    continue
                parts = path.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    folders.add("/".join(parts[:i]))
                # Only files actually selected for download (priority != 0) —
                # same signal the remux pass itself uses to mean "kept" — so
                # deselected extras/samples don't clutter the list.
                if entry_obj.get("priority") != 0:
                    names.append(path.rsplit("/", 1)[-1])

        return web.json_response({
            "ok": True,
            "files": sorted(names),
            "is_bluray": is_bluray_structure(folders),
        })


class QbitAirdropForceStartView(HomeAssistantView):
    url = "/api/qbit_airdrop/force_start"
    name = "qbit_airdrop:force_start"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

    async def post(self, request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        thash = (data.get("hash") or "").strip().lower()
        if not thash:
            return web.json_response({"ok": False, "error": "hash required"}, status=400)

        value = bool(data.get("value", True))
        (base,) = _resolve_base(self.entry)
        if not base:
            return web.json_response({"ok": False, "error": "qB base not configured"}, status=400)

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{base}/api/v2/torrents/setForceStart",
                data={"hashes": thash, "value": "true" if value else "false"},
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    _LOGGER.error("Force start failed: %s %s", resp.status, txt[:200])
                    return web.json_response({"ok": False, "error": "Force start failed"}, status=resp.status)
        except ClientError as err:
            _LOGGER.error("qB POST error: %s", err)
            return web.json_response({"ok": False, "error": "Request error"}, status=502)

        return web.json_response({"ok": True})