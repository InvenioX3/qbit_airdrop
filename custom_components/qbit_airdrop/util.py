from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry

from .const import CONF_HOST, CONF_PORT


def resolve_base(entry: ConfigEntry) -> Tuple[str]:
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
