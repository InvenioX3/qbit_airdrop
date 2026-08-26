from __future__ import annotations

import json
import logging

import asyncssh
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION, STORAGE_KEY_SSH_KEY_FMT

_LOGGER = logging.getLogger(__name__)


async def get_or_create_keypair(hass: HomeAssistant, entry_id: str) -> tuple[str, str]:
    """Returns (private_key_pem, public_key_openssh), generating an ed25519
    keypair on first call and persisting it. The private key never leaves
    HA's own storage; the public key is what gets added to the qBittorrent
    host's authorized_keys."""
    key_store = Store(hass, STORAGE_VERSION, STORAGE_KEY_SSH_KEY_FMT.format(entry_id))
    data = await key_store.async_load()
    if data and data.get("private_key") and data.get("public_key"):
        return data["private_key"], data["public_key"]

    key = asyncssh.generate_private_key("ssh-ed25519")
    private_key = key.export_private_key().decode()
    public_key = key.export_public_key().decode().strip()

    await key_store.async_save({"private_key": private_key, "public_key": public_key})
    return private_key, public_key


def _track_lang(track: dict) -> str | None:
    props = track.get("properties") or {}
    lang_ietf = str(props.get("language_ietf") or "").strip().lower()
    if lang_ietf:
        return lang_ietf
    lang = str(props.get("language") or "").strip().lower()
    return lang or None


def _is_english(lang: str | None) -> bool:
    return lang in ("en", "eng")


def _is_undefined(lang: str | None) -> bool:
    return lang is None or lang in ("und", "")


def plan_tracks(identify: dict) -> dict:
    """Pure decision logic over mkvmerge -J output.

    Keep English-tagged subtitles, strip the rest. Keep only English-tagged
    audio if any exists, otherwise leave every audio track untouched. Any
    track (audio or subtitle) with an undefined language skips the file
    entirely rather than guessing.

    Returns {"skip": bool, "audio_keep_ids": list[int] | None, "subtitle_keep_ids": list[int]}.
    audio_keep_ids of None means "keep everything" (the --audio-tracks flag
    is omitted rather than passed).
    """
    tracks = identify.get("tracks") or []
    audio = [t for t in tracks if t.get("type") == "audio"]
    subtitles = [t for t in tracks if t.get("type") == "subtitles"]

    for t in audio + subtitles:
        if _is_undefined(_track_lang(t)):
            return {"skip": True, "audio_keep_ids": None, "subtitle_keep_ids": []}

    english_audio_ids = [t["id"] for t in audio if _is_english(_track_lang(t))]
    english_subtitle_ids = [t["id"] for t in subtitles if _is_english(_track_lang(t))]

    return {
        "skip": False,
        "audio_keep_ids": english_audio_ids or None,
        "subtitle_keep_ids": english_subtitle_ids,
    }


def build_remux_command(mkvmerge_path: str, source_path: str, dest_path: str, plan: dict) -> str:
    parts = [f'"{mkvmerge_path}"', "-o", f'"{dest_path}"']

    if plan["audio_keep_ids"] is not None:
        parts += ["--audio-tracks", ",".join(str(i) for i in plan["audio_keep_ids"])]

    if plan["subtitle_keep_ids"]:
        parts += ["--subtitle-tracks", ",".join(str(i) for i in plan["subtitle_keep_ids"])]
    else:
        parts.append("--no-subtitles")

    parts.append(f'"{source_path}"')
    return " ".join(parts)


async def remux_file(
    host: str,
    ssh_port: int,
    username: str,
    private_key_pem: str,
    mkvmerge_path: str,
    source_path: str,
    dest_path: str,
) -> tuple[bool, bool]:
    """Runs entirely on the remote Windows host over SSH — identify, decide,
    then remux straight to dest_path. Returns (success, skipped).

    Assumes the SSH server's default shell is cmd.exe (Windows OpenSSH
    Server's own default) for the mkdir precheck; if that host has been
    reconfigured to a different DefaultShell, the mkdir command below will
    need adjusting."""
    try:
        client_key = asyncssh.import_private_key(private_key_pem)
        async with asyncssh.connect(
            host, port=ssh_port, username=username,
            client_keys=[client_key], known_hosts=None,
        ) as conn:
            identify_cmd = f'"{mkvmerge_path}" -J "{source_path}"'
            result = await conn.run(identify_cmd, check=False)
            if result.exit_status != 0:
                _LOGGER.warning(
                    "[QBIT] remux: identify failed source=%s exit=%s stderr=%s",
                    source_path, result.exit_status, result.stderr,
                )
                return False, False

            try:
                identify = json.loads(result.stdout)
            except ValueError:
                _LOGGER.warning(
                    "[QBIT] remux: could not parse mkvmerge -J output source=%s",
                    source_path,
                )
                return False, False

            plan = plan_tracks(identify)
            if plan["skip"]:
                _LOGGER.warning(
                    "[QBIT] remux: undefined-language track present, skipping source=%s",
                    source_path,
                )
                return False, True

            dest_dir = dest_path.rsplit("\\", 1)[0] if "\\" in dest_path else dest_path
            await conn.run(f'if not exist "{dest_dir}" mkdir "{dest_dir}"', check=False)

            remux_cmd = build_remux_command(mkvmerge_path, source_path, dest_path, plan)
            result = await conn.run(remux_cmd, check=False)
            # mkvmerge exit codes: 0 = success, 1 = success with warnings, 2 = error
            if result.exit_status not in (0, 1):
                _LOGGER.warning(
                    "[QBIT] remux: mkvmerge failed source=%s exit=%s stderr=%s",
                    source_path, result.exit_status, result.stderr,
                )
                return False, False

            _LOGGER.debug("[QBIT] remux: wrote %s", dest_path)
            return True, False
    except (OSError, asyncssh.Error):
        _LOGGER.exception("[QBIT] remux: SSH error host=%s source=%s", host, source_path)
        return False, False
