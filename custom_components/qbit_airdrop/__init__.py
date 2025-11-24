from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse

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


async def async_setup(hass: HomeAssistant, config) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # HTTP views
    from .http import QbitAirdropActiveView, QbitAirdropDeleteView

    hass.http.register_view(QbitAirdropActiveView(hass, entry))
    hass.http.register_view(QbitAirdropDeleteView(hass, entry))

    session = aiohttp_client.async_get_clientsession(hass)

    async def add_magnet(call: ServiceCall) -> None:
        data = call.data or {}
        magnet = (data.get("magnet") or "").strip()
        category = (data.get("category") or "").strip()
        if not magnet:
            return

        (base,) = _resolve_base(entry)
        if not base:
            return

        base_path = _resolve_base_path(entry)

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
