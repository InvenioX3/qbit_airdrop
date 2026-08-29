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

# The qBittorrent/mkvmerge host's OS — always the same machine (confirmed:
# mkvmerge runs on the same client hosting qBittorrent), so one setting
# covers path-separator choice, shell syntax for the remux's mkdir
# precheck, and whether the Windows-only NAS "net use" step applies at all.
CONF_MKVMERGE_HOST_OS = "mkvmerge_host_os"
DEFAULT_MKVMERGE_HOST_OS = "windows"
MKVMERGE_HOST_OS_CHOICES = [
    {"value": "windows", "label": "Windows"},
    {"value": "linux", "label": "Linux / other (POSIX)"},
]

# NAS credentials for the remux destination share — SSH's own network-logon
# session can't use Windows Credential Manager, so this is authenticated
# explicitly (net use \\host\IPC$) each remux rather than relying on any
# cached/persisted credential on the host.
CONF_NAS_USERNAME = "nas_username"
CONF_NAS_PASSWORD = "nas_password"

# Languages to retain audio/subtitle tracks for during remux. Stored as
# ISO 639-2 codes (matching mkvmerge's own "language" property); each entry
# below also carries its ISO 639-1 (two-letter) equivalent since mkvmerge's
# newer "language_ietf" property reports that form instead — track matching
# checks a file's reported language against both forms for every selected
# entry, not just the three-letter one actually stored in config.
CONF_RETAIN_LANGUAGES = "retain_languages"
DEFAULT_RETAIN_LANGUAGES = ["eng"]

# Separate selector for subtitles — independent from audio retention, since
# a user may want e.g. English+Spanish audio kept but only English subs.
# Only tracks matching this list are kept, with no undefined-language
# exception (unlike audio, where an undefined track is always retained).
CONF_RETAIN_SUBTITLE_LANGUAGES = "retain_subtitle_languages"
DEFAULT_RETAIN_SUBTITLE_LANGUAGES = ["eng"]

# OpenSubtitles.com — optional. Feature is only active once an API key,
# account login, and at least one subtitle-retention language are all
# configured; any missing piece just leaves subtitle fetching disabled.
CONF_OPENSUBTITLES_API_KEY = "opensubtitles_api_key"
CONF_OPENSUBTITLES_USERNAME = "opensubtitles_username"
CONF_OPENSUBTITLES_PASSWORD = "opensubtitles_password"

LANGUAGE_CHOICES = [
    {"code": "eng", "code2": "en", "label": "English"},
    {"code": "spa", "code2": "es", "label": "Spanish"},
    {"code": "fre", "code2": "fr", "label": "French"},
    {"code": "ger", "code2": "de", "label": "German"},
    {"code": "ita", "code2": "it", "label": "Italian"},
    {"code": "por", "code2": "pt", "label": "Portuguese"},
    {"code": "rus", "code2": "ru", "label": "Russian"},
    {"code": "jpn", "code2": "ja", "label": "Japanese"},
    {"code": "kor", "code2": "ko", "label": "Korean"},
    {"code": "chi", "code2": "zh", "label": "Chinese"},
    {"code": "ara", "code2": "ar", "label": "Arabic"},
    {"code": "hin", "code2": "hi", "label": "Hindi"},
    {"code": "dut", "code2": "nl", "label": "Dutch"},
    {"code": "swe", "code2": "sv", "label": "Swedish"},
    {"code": "nor", "code2": "no", "label": "Norwegian"},
    {"code": "dan", "code2": "da", "label": "Danish"},
    {"code": "fin", "code2": "fi", "label": "Finnish"},
    {"code": "pol", "code2": "pl", "label": "Polish"},
    {"code": "tur", "code2": "tr", "label": "Turkish"},
    {"code": "gre", "code2": "el", "label": "Greek"},
    {"code": "heb", "code2": "he", "label": "Hebrew"},
    {"code": "tha", "code2": "th", "label": "Thai"},
    {"code": "vie", "code2": "vi", "label": "Vietnamese"},
    {"code": "ukr", "code2": "uk", "label": "Ukrainian"},
    {"code": "cze", "code2": "cs", "label": "Czech"},
    {"code": "hun", "code2": "hu", "label": "Hungarian"},
    {"code": "rum", "code2": "ro", "label": "Romanian"},
    {"code": "ind", "code2": "id", "label": "Indonesian"},
    {"code": "may", "code2": "ms", "label": "Malay"},
]

# qBittorrent tag used to mark remux outcome
TAG_REMUXED = "Remuxed"

# Persistent per-torrent record storage
STORAGE_VERSION = 1
STORAGE_KEY_TORRENTS_FMT = "qbit_airdrop_{}_torrents"
STORAGE_KEY_SSH_KEY_FMT = "qbit_airdrop_{}_ssh_key"