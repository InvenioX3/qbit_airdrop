from __future__ import annotations

import asyncio
import json
import logging

import asyncssh
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION, STORAGE_KEY_SSH_KEY_FMT

_LOGGER = logging.getLogger(__name__)

# A hung SSH connect/command would otherwise block indefinitely — and since
# every remux runs under the same shared poll lock as renaming/cleanup, an
# unbounded hang here silently wedges the entire integration, not just
# remuxing. Every network call below is bounded so a stuck operation fails
# loudly and lets the next poll tick retry instead.
_CONNECT_TIMEOUT = 30
_QUICK_CMD_TIMEOUT = 30
_REMUX_TIMEOUT = 7200  # large 4K remuxes can legitimately take a while


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
    audio if any exists, otherwise leave every audio track untouched.

    An undefined-language track only triggers a skip when there's no
    English-tagged track anywhere in the file (audio or subtitle) — that's
    the only genuinely ambiguous case, where it's unclear whether this is
    English content with incomplete tags or truly foreign content. Once any
    track in the file is confirmed English, the file is known to be English
    content, and any other undefined track is just treated the same as a
    known non-English one — dropped, no further ambiguity.

    Returns {"skip": bool, "audio_keep_ids": list[int] | None, "subtitle_keep_ids": list[int]}.
    audio_keep_ids of None means "keep everything" (the --audio-tracks flag
    is omitted rather than passed).
    """
    tracks = identify.get("tracks") or []
    audio = [t for t in tracks if t.get("type") == "audio"]
    subtitles = [t for t in tracks if t.get("type") == "subtitles"]

    english_audio_ids = [t["id"] for t in audio if _is_english(_track_lang(t))]
    english_subtitle_ids = [t["id"] for t in subtitles if _is_english(_track_lang(t))]

    if not english_audio_ids and not english_subtitle_ids:
        # No confirmed English track anywhere. If anything's language is
        # genuinely unknown, don't guess whether this is English content
        # with incomplete tags — skip. Otherwise everything is confidently
        # tagged as some known non-English language: leave audio untouched
        # (nothing English to prefer) and drop subtitles (no English ones
        # to keep) — the Korean/Chinese-style case.
        if any(_is_undefined(_track_lang(t)) for t in audio + subtitles):
            return {"skip": True, "audio_keep_ids": None, "subtitle_keep_ids": []}
        return {"skip": False, "audio_keep_ids": None, "subtitle_keep_ids": []}

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


def _unc_host(path: str) -> str:
    if not path.startswith("\\\\"):
        return ""
    return path.lstrip("\\").split("\\", 1)[0]


async def remux_file(
    host: str,
    ssh_port: int,
    username: str,
    private_key_pem: str,
    mkvmerge_path: str,
    source_path: str,
    dest_path: str,
    nas_username: str = "",
    nas_password: str = "",
) -> tuple[bool, bool]:
    """Runs entirely on the remote Windows host over SSH — identify, decide,
    then remux straight to dest_path. Returns (success, skipped).

    Assumes the SSH server's default shell is cmd.exe (Windows OpenSSH
    Server's own default) for the mkdir precheck; if that host has been
    reconfigured to a different DefaultShell, the mkdir command below will
    need adjusting.

    SSH sessions on Windows are network logons and can't use Windows
    Credential Manager (cmdkey), so if the destination is a UNC path and
    NAS credentials are configured, authenticate to that server explicitly
    via `net use \\host\\IPC$` before writing — this works from a network
    logon since the password is passed inline rather than relying on any
    cached/persisted credential."""
    try:
        client_key = asyncssh.import_private_key(private_key_pem)
        _LOGGER.debug(
            "[QBIT] remux: connecting host=%s port=%s user=%s",
            host, ssh_port, username,
        )
        conn = await asyncio.wait_for(
            asyncssh.connect(
                host, port=ssh_port, username=username,
                client_keys=[client_key], known_hosts=None,
            ),
            timeout=_CONNECT_TIMEOUT,
        )
        async with conn:
            identify_cmd = f'"{mkvmerge_path}" -J "{source_path}"'
            _LOGGER.debug("[QBIT] remux: identify command=%s", identify_cmd)
            result = await asyncio.wait_for(
                conn.run(identify_cmd, check=False), timeout=_QUICK_CMD_TIMEOUT,
            )
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

            track_summary = [
                {
                    "id": t.get("id"),
                    "type": t.get("type"),
                    "language": (t.get("properties") or {}).get("language"),
                    "language_ietf": (t.get("properties") or {}).get("language_ietf"),
                }
                for t in (identify.get("tracks") or [])
            ]
            _LOGGER.warning(
                "[QBIT] remux: tracks for %s: %s",
                source_path, track_summary,
            )

            plan = plan_tracks(identify)
            if plan["skip"]:
                _LOGGER.warning(
                    "[QBIT] remux: undefined-language track present, skipping source=%s",
                    source_path,
                )
                return False, True

            nas_host = _unc_host(dest_path)
            if nas_host and nas_username:
                unc_root = f"\\\\{nas_host}\\IPC$"
                net_use_cmd = f'net use "{unc_root}" /user:{nas_username} {nas_password}'
                redacted_cmd = f'net use "{unc_root}" /user:{nas_username} ***'
                _LOGGER.debug("[QBIT] remux: net use command=%s", redacted_cmd)
                net_use_result = await asyncio.wait_for(
                    conn.run(net_use_cmd, check=False), timeout=_QUICK_CMD_TIMEOUT,
                )
                _LOGGER.warning(
                    "[QBIT] remux: net use %s exit=%s stdout=%r stderr=%r",
                    unc_root, net_use_result.exit_status, net_use_result.stdout, net_use_result.stderr,
                )

            dest_dir = dest_path.rsplit("\\", 1)[0] if "\\" in dest_path else dest_path
            mkdir_cmd = f'if not exist "{dest_dir}" mkdir "{dest_dir}"'
            _LOGGER.debug("[QBIT] remux: mkdir command=%s", mkdir_cmd)
            mkdir_result = await asyncio.wait_for(
                conn.run(mkdir_cmd, check=False), timeout=_QUICK_CMD_TIMEOUT,
            )
            _LOGGER.warning(
                "[QBIT] remux: mkdir precheck dir=%s exit=%s stdout=%r stderr=%r",
                dest_dir, mkdir_result.exit_status, mkdir_result.stdout, mkdir_result.stderr,
            )

            remux_cmd = build_remux_command(mkvmerge_path, source_path, dest_path, plan)
            _LOGGER.debug("[QBIT] remux: command=%s", remux_cmd)
            result = await asyncio.wait_for(
                conn.run(remux_cmd, check=False), timeout=_REMUX_TIMEOUT,
            )
            # mkvmerge exit codes: 0 = success, 1 = success with warnings, 2 = error
            if result.exit_status not in (0, 1):
                _LOGGER.warning(
                    "[QBIT] remux: mkvmerge failed source=%s exit=%s stderr=%s",
                    source_path, result.exit_status, result.stderr,
                )
                return False, False

            _LOGGER.debug("[QBIT] remux: wrote %s", dest_path)
            return True, False
    except asyncio.TimeoutError:
        _LOGGER.error(
            "[QBIT] remux: timed out host=%s source=%s — treating as a failure, will retry next pass",
            host, source_path,
        )
        return False, False
    except (OSError, asyncssh.Error):
        _LOGGER.exception("[QBIT] remux: SSH error host=%s source=%s", host, source_path)
        return False, False
