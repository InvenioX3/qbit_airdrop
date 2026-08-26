from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_VERSION, STORAGE_KEY_TORRENTS_FMT

# Records live from add_magnet through manual deletion — spanning the
# pending-rename queue (old behavior), the download itself, and the wait for
# remux. Persisted so an HA restart mid-download doesn't strand a torrent
# with no way to recover its destination path or category.
#
# record shape:
# {
#   "category": str,        # show name, "" for movie
#   "token_type": str,      # se/s/season/complete/year/blank
#   "season": str,
#   "rename_name": str,
#   "stage": "pending" | "awaiting_remux",
#   "added_at": datetime,
#   "last_checked_at": datetime | None,
# }
#
# The remux destination is intentionally NOT precomputed/stored here —
# setLocation already places each torrent at Qbittorrent default save
# path\category[\season] (or \<movie name> for movies), so the remux pass
# derives both source and destination fresh from qBittorrent's own live
# state plus category/token_type/season at the time it actually runs.


class TorrentStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_TORRENTS_FMT.format(entry_id))
        self.torrents: dict[str, dict] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        torrents = (data or {}).get("torrents") or {}
        loaded = {}
        for torrent_hash, rec in torrents.items():
            rec = dict(rec)
            rec["added_at"] = dt_util.parse_datetime(rec.get("added_at") or "") or dt_util.utcnow()
            last_checked = rec.get("last_checked_at")
            rec["last_checked_at"] = dt_util.parse_datetime(last_checked) if last_checked else None
            loaded[torrent_hash] = rec
        self.torrents = loaded

    async def async_save(self) -> None:
        serialized = {}
        for torrent_hash, rec in self.torrents.items():
            rec = dict(rec)
            rec["added_at"] = rec["added_at"].isoformat()
            rec["last_checked_at"] = (
                rec["last_checked_at"].isoformat() if rec.get("last_checked_at") else None
            )
            serialized[torrent_hash] = rec
        await self._store.async_save({"torrents": serialized})
