from __future__ import annotations

DOMAIN = "qbit_airdrop"

# Connection
CONF_HOST = "host"
CONF_PORT = "port"
CONF_BASE_PATH = "base_path"
CONF_DOWNLOAD_PATH = "download_path"
CONF_MOVIE_PATH = "movie_path"
CONF_TEMP_HA_PATH = "temp_ha_path"
CONF_CONFIRM_DELETE = "confirm_delete"

# Remux (SSH to the qBittorrent host, which also runs mkvmerge)
CONF_SSH_PORT = "ssh_port"
CONF_SSH_USERNAME = "ssh_username"
CONF_MKVMERGE_PATH = "mkvmerge_path"

# NAS credentials for the remux destination share — SSH's own network-logon
# session can't use Windows Credential Manager, so this is authenticated
# explicitly (net use \\host\IPC$) each remux rather than relying on any
# cached/persisted credential on the host.
CONF_NAS_USERNAME = "nas_username"
CONF_NAS_PASSWORD = "nas_password"

# qBittorrent tags used to mark remux outcome
TAG_REMUXED = "Remuxed"
TAG_REMUX_SKIPPED = "Remux skipped - language undefined"

# Persistent per-torrent record storage
STORAGE_VERSION = 1
STORAGE_KEY_TORRENTS_FMT = "qbit_airdrop_{}_torrents"
STORAGE_KEY_SSH_KEY_FMT = "qbit_airdrop_{}_ssh_key"