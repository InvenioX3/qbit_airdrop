from __future__ import annotations

import json
import logging
from typing import List, Tuple
from urllib.parse import urlparse

from aiohttp import ClientError, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOST, CONF_PORT

_LOGGER = logging.getLogger(__name__)


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
        base = f"{parsed.scheme}://{netloc}"
        if ":" not in netloc:
            base = f"{base}:{port}"
    return (base.rstrip("/"),)


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

        items: List[dict] = []
        if isinstance(payload, list):
            for obj in payload:
                name = str(obj.get("name") or "").strip()
                prog = obj.get("progress", None)
                try:
                    pct = int(round(float(prog) * 100)) if prog is not None else None
                except Exception:
                    pct = None
                if pct is not None:
                    pct = max(0, min(100, pct))

                items.append({
                    "title": name,
                    "percent": pct,
                    "hash": str(obj.get("hash") or "").lower(),
                    "state": str(obj.get("state") or "").lower(),
                    "size": obj.get("size", None),
                    # pass-through for the card
                    "dlspeed": obj.get("dlspeed", 0),           # bytes/sec
                    "upspeed": obj.get("upspeed", 0),           # bytes/sec
                    "availability": obj.get("availability", None)
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

        return web.json_response({"ok": True})
