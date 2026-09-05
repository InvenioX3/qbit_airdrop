from __future__ import annotations

import os
import re
from typing import Tuple
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry

from .const import CONF_HOST, CONF_PORT


def base_from_data(data: dict) -> Tuple[str]:
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


def resolve_base(entry: ConfigEntry) -> Tuple[str]:
    return base_from_data(entry.options or entry.data or {})


# Raw Blu-ray disc dumps (BDMV/STREAM structure) vary too much to classify
# file-by-file without running the actual disc index through mkvmerge, which
# isn't possible until the whole torrent has downloaded. Detected by folder
# name alone so both the queue processor and the file-list endpoint agree on
# what counts as "a Blu-ray disc" without duplicating the check.
BLURAY_MARKERS = {"bdmv", "!any", "certificate"}


def is_bluray_structure(folders) -> bool:
    for folder in folders:
        for segment in folder.split("/"):
            if segment.strip().lower() in BLURAY_MARKERS:
                return True
    return False


# Shared between the queue processor (renaming) and the file-list endpoint
# (grouping subtitles under their video) so both agree on what counts as a
# video/subtitle file and how an episode code is derived from a filename.
VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv", ".iso",
}

# Substring search rather than a strict suffix check — real-world releases
# sometimes append tracker-signature junk after the true extension (e.g.
# "...en.srt[eztv.re]"), which a plain os.path.splitext would miss entirely.
# Neither string realistically appears anywhere in a non-subtitle filename.
SUBTITLE_EXT_RE = re.compile(r"\.(srt|ass)", re.I)

_EPISODE_TOKEN_RE = re.compile(r"\bS(\d{1,2})[.\s_-]?((?:E\d{1,3})+)\b", re.I)
EPISODE_NUM_RE = re.compile(r"E(\d{1,3})", re.I)


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def is_subtitle_file(path: str) -> bool:
    return bool(SUBTITLE_EXT_RE.search(path))


def detect_episode(name: str) -> str:
    match = _EPISODE_TOKEN_RE.search(name)
    if not match:
        return ""
    season_num = int(match.group(1))
    episode_nums = [int(n) for n in EPISODE_NUM_RE.findall(match.group(2))]
    episodes = "".join(f"E{n:02d}" for n in episode_nums)
    return f"S{season_num:02d}{episodes}"


def subtitle_episode_code(sub_rel_path: str) -> str:
    """The subtitle's own filename usually carries the episode code
    directly, but some releases instead use a generic/numbered subtitle
    filename (e.g. "2_English.srt") inside a folder named after the video
    it belongs to — falls back to the immediate parent folder's name when
    the filename itself yields nothing."""
    episode = detect_episode(os.path.basename(sub_rel_path))
    if episode:
        return episode
    if "/" in sub_rel_path:
        parent_leaf = sub_rel_path.rsplit("/", 1)[0].rsplit("/", 1)[-1]
        return detect_episode(parent_leaf)
    return ""
