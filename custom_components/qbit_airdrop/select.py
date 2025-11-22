from __future__ import annotations

import json
import logging
from typing import Optional, List

from aiohttp import ClientError
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOST, CONF_PORT, CONF_USERNAME, CONF_PASSWORD

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    ent = QbitAirdropCategorySelect(hass, entry)
    async_add_entities([ent], True)
    platform = async_get_current_platform()
    platform.async_register_entity_service("refresh_options", {}, "async_refresh_options")

class QbitAirdropCategorySelect(SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "Qbit Airdrop Category"
    _attr_icon = "mdi:format-list-bulleted"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_category"
        self._options: List[str] = []
        self._selected: Optional[str] = None

    @property
    def options(self) -> list[str]:
        return self._options

    @property
    def current_option(self) -> Optional[str]:
        return self._selected

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            self._selected = last.state
        await self.async_refresh_options()

    async def async_select_option(self, option: str) -> None:
        if option in self._options:
            self._selected = option
            self.async_write_ha_state()

    async def async_refresh_options(self) -> None:
        data = self.entry.options or self.entry.data or {}
        host = (data.get("host") or "").strip().rstrip("/")
        port = data.get("port", 8080)
        username = (data.get("username") or "")
        password = (data.get("password") or "")
        if not host:
            _LOGGER.warning("No qBittorrent host configured")
            self._options = []
            self.async_write_ha_state()
            return
        base = f"http://{host}:{port}"
        session = async_get_clientsession(self.hass)
        if username and password:
            try:
                async with session.post(f"{base}/api/v2/auth/login", data={"username": username, "password": password}, timeout=10) as resp:
                    await resp.text()
                    if resp.status != 200:
                        _LOGGER.warning("qBittorrent login failed: %s %s", resp.status, await resp.text())
                        self._options = []
                        self.async_write_ha_state()
                        return
            except Exception as err:
                _LOGGER.warning("qBittorrent login error: %s", err)
                self._options = []
                self.async_write_ha_state()
                return
        try:
            async with session.get(f"{base}/api/v2/torrents/categories", timeout=10) as resp:
                body = await resp.text()
                if resp.status != 200:
                    _LOGGER.warning("Fetching categories failed: %s %s", resp.status, body)
                    self._options = []
                    self.async_write_ha_state()
                    return
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = json.loads(body)
                names: List[str] = []
                if isinstance(payload, dict):
                    names = sorted([str(k) for k in payload.keys()])
                elif isinstance(payload, list):
                    names = sorted(str(x) for x in payload if isinstance(x, (str,int)))
                self._options = names
                if self._selected not in self._options:
                    self._selected = self._options[0] if self._options else None
        except ClientError as e:
            _LOGGER.warning("qBittorrent categories request error: %s", e)
            self._options = []
        self.async_write_ha_state()
