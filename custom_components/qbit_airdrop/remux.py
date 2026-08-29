from __future__ import annotations

import asyncio
import json
import logging

import asyncssh
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    STORAGE_VERSION,
    STORAGE_KEY_SSH_KEY_FMT,
    LANGUAGE_CHOICES,
    DEFAULT_RETAIN_LANGUAGES,
    DEFAULT_RETAIN_SUBTITLE_LANGUAGES,
)

_LOGGER = logging.getLogger(__name__)

# Each configured language (stored as its ISO 639-2 code) also needs to match
# mkvmerge's newer "language_ietf" property, which reports the ISO 639-1
# (two-letter) form instead — expand every selected code to both forms once,
# so track matching is a simple set-containment check.
_CODE_EXPANSIONS = {c["code"]: {c["code"], c["code2"]} for c in LANGUAGE_CHOICES}

# Fallback display name for a kept subtitle track when it carries neither a
# commentary nor hearing-impaired flag — those flags are optional Matroska
# metadata a release may simply never have set.
_LANG_LABEL_BY_CODE: dict[str, str] = {}
for _c in LANGUAGE_CHOICES:
    _LANG_LABEL_BY_CODE[_c["code"]] = _c["label"]
    _LANG_LABEL_BY_CODE[_c["code2"]] = _c["label"]

_COMMENTARY_LABEL = "Commentary"
_HEARING_IMPAIRED_LABEL = "SDH"
_OPENSUBTITLES_INJECTED_LABEL = "(OpenSubtitles->Qbit)"
_TORRENT_INJECTED_LABEL = "(Torrent->Qbit)"


def _expand_retain_codes(retain_codes: list[str]) -> set[str]:
    expanded: set[str] = set()
    for code in retain_codes or DEFAULT_RETAIN_LANGUAGES:
        expanded |= _CODE_EXPANSIONS.get(code, {code})
    return expanded


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


def _is_undefined(lang: str | None) -> bool:
    return lang is None or lang in ("und", "")


def plan_tracks(
    identify: dict,
    retain_audio_codes: list[str] | None = None,
    retain_subtitle_codes: list[str] | None = None,
) -> dict:
    """Pure decision logic over mkvmerge -J output.

    Audio: keep every track tagged with a configured audio language, unioned
    with every undefined-language track — an audio track with no language
    info is always kept, unconditionally, regardless of what else is
    present. Only a track confidently tagged as some other, non-configured
    language gets stripped. If nothing is configured-language anywhere,
    audio is left fully untouched (the Korean/Chinese-only-audio case).

    Subtitles: kept only if they match the (separate) subtitle-retention
    list — no undefined-language exception here, unlike audio. Anything
    else, including undefined-language subtitles, gets stripped.

    Returns {
      "audio_keep_ids": list[int] | None,   # None means keep everything
      "subtitle_keep_ids": list[int],
      "missing_subtitle_langs": list[str],  # configured codes with no matching embedded track
    }
    """
    retained_audio = _expand_retain_codes(retain_audio_codes or DEFAULT_RETAIN_LANGUAGES)
    retained_subs = _expand_retain_codes(retain_subtitle_codes or DEFAULT_RETAIN_SUBTITLE_LANGUAGES)

    tracks = identify.get("tracks") or []
    audio = [t for t in tracks if t.get("type") == "audio"]
    subtitles = [t for t in tracks if t.get("type") == "subtitles"]

    retained_audio_ids = [t["id"] for t in audio if _track_lang(t) in retained_audio]
    undefined_audio_ids = [t["id"] for t in audio if _is_undefined(_track_lang(t))]

    if retained_audio_ids:
        audio_keep_ids = sorted(set(retained_audio_ids) | set(undefined_audio_ids))
    else:
        audio_keep_ids = None

    subtitle_keep_ids = [t["id"] for t in subtitles if _track_lang(t) in retained_subs]

    missing_subtitle_langs = []
    for code in (retain_subtitle_codes or DEFAULT_RETAIN_SUBTITLE_LANGUAGES):
        expansion = _CODE_EXPANSIONS.get(code, {code})
        if not any(_track_lang(t) in expansion for t in subtitles):
            missing_subtitle_langs.append(code)

    return {
        "audio_keep_ids": audio_keep_ids,
        "subtitle_keep_ids": subtitle_keep_ids,
        "missing_subtitle_langs": missing_subtitle_langs,
    }


def _subtitle_title(track: dict) -> str:
    props = track.get("properties") or {}

    labels = []
    if props.get("flag_commentary"):
        labels.append(_COMMENTARY_LABEL)
    if props.get("flag_hearing_impaired"):
        labels.append(_HEARING_IMPAIRED_LABEL)
    if labels:
        return ", ".join(labels)

    # The flags above are frequently just never set by whatever encoded the
    # file — fall back to pattern-matching the track's own existing title
    # for these two standardized English terms specifically (not a general
    # translation/parsing attempt — just checking whether the encoder
    # already wrote one of these two words verbatim).
    existing_name = str(props.get("track_name") or "").lower()
    text_labels = []
    if "commentary" in existing_name:
        text_labels.append(_COMMENTARY_LABEL)
    if "sdh" in existing_name:
        text_labels.append(_HEARING_IMPAIRED_LABEL)
    if text_labels:
        return ", ".join(text_labels)

    lang = _track_lang(track)
    return _LANG_LABEL_BY_CODE.get(lang, lang or "")


def compute_track_names(identify: dict, plan: dict, video_title: str | None) -> dict[int, str]:
    """Track ID -> desired --track-name value, for tracks already embedded
    in the source file (injected/fetched subtitles are named separately,
    since they aren't part of this identify data at all).

    Video naming only applies when video_title is given (movies only — TV
    episode titles are left untouched, since there's no reliable per-episode
    title available). Audio naming uses mkvmerge's own reported codec string
    verbatim — the same clean name ("AC-3", "DTS", "TrueHD", ...) the GUI
    itself shows, not anything hand-parsed. Subtitle naming uses the
    commentary/hearing-impaired flags when the source actually set them —
    those are real Matroska properties, but optional ones a release may
    never have populated. When absent, falls back to pattern-matching the
    track's own existing title for those same two words, then finally to
    the language name if neither signal is present."""
    names: dict[int, str] = {}
    tracks = identify.get("tracks") or []

    if video_title:
        for t in tracks:
            if t.get("type") == "video":
                names[t["id"]] = video_title

    audio_ids = plan["audio_keep_ids"]
    for t in tracks:
        if t.get("type") != "audio":
            continue
        if audio_ids is not None and t["id"] not in audio_ids:
            continue
        codec = t.get("codec")
        if codec:
            names[t["id"]] = codec

    for t in tracks:
        if t.get("type") != "subtitles":
            continue
        if t["id"] not in plan["subtitle_keep_ids"]:
            continue
        title = _subtitle_title(t)
        if title:
            names[t["id"]] = title

    return names


def injected_subtitle_track_name(source: str = "opensubtitles") -> str:
    # No language repeated here deliberately — the media player already
    # derives and displays a human-readable language name from the track's
    # --language tag itself, so including it again in the track name just
    # produced duplicated, cluttered labels like "English - English (...)".
    return _TORRENT_INJECTED_LABEL if source == "torrent" else _OPENSUBTITLES_INJECTED_LABEL


def build_remux_command(
    mkvmerge_path: str, source_path: str, dest_path: str, plan: dict,
    track_names: dict[int, str] | None = None,
    extra_subtitles: list[dict] | None = None,
) -> str:
    """extra_subtitles: list of {"path": remote_srt_path, "lang": iso639-2
    code, "track_name": display name} — each gets muxed in as its own
    additional input file, with its own --language/--track-name flags
    (a standalone .srt/.ass has exactly one track, always id 0 within that
    file) applied immediately before its own path on the command line."""
    parts = [f'"{mkvmerge_path}"', "-o", f'"{dest_path}"']

    for track_id, name in (track_names or {}).items():
        safe_name = name.replace('"', "")
        parts += ["--track-name", f'"{track_id}:{safe_name}"']

    if plan["audio_keep_ids"] is not None:
        parts += ["--audio-tracks", ",".join(str(i) for i in plan["audio_keep_ids"])]

    if plan["subtitle_keep_ids"]:
        parts += ["--subtitle-tracks", ",".join(str(i) for i in plan["subtitle_keep_ids"])]
    else:
        parts.append("--no-subtitles")

    parts.append(f'"{source_path}"')

    for extra in (extra_subtitles or []):
        safe_name = extra["track_name"].replace('"', "")
        parts += ["--language", f'"0:{extra["lang"]}"']
        parts += ["--track-name", f'"0:{safe_name}"']
        parts.append(f'"{extra["path"]}"')

    return " ".join(parts)


def _unc_host(path: str) -> str:
    if not path.startswith("\\\\"):
        return ""
    return path.lstrip("\\").split("\\", 1)[0]


async def open_connection(host: str, ssh_port: int, username: str, private_key_pem: str):
    """Opens one SSH connection, meant to be reused by the caller across
    every file in a torrent rather than opened fresh per file — opening a
    new connection per file was the likely trigger for a server-side
    connection-rate limit (e.g. Windows OpenSSH Server's MaxStartups) once
    several files in a season pack were processed back-to-back: the first
    few would succeed and every one after would hang until a bounded
    timeout kicked in. Returns None on failure; the caller should skip this
    torrent for the current pass and retry next tick."""
    try:
        client_key = asyncssh.import_private_key(private_key_pem)
        _LOGGER.debug(
            "[QBIT] remux: connecting host=%s port=%s user=%s",
            host, ssh_port, username,
        )
        return await asyncio.wait_for(
            asyncssh.connect(
                host, port=ssh_port, username=username,
                client_keys=[client_key], known_hosts=None,
            ),
            timeout=_CONNECT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _LOGGER.error("[QBIT] remux: connect timed out host=%s", host)
        return None
    except (OSError, asyncssh.Error):
        _LOGGER.exception("[QBIT] remux: SSH connect error host=%s", host)
        return None


async def close_connection(conn, host: str) -> None:
    """Bounded, best-effort close — a hung teardown gets logged and
    abandoned rather than blocking indefinitely, since that was previously
    capable of silently wedging the shared poll lock for a long time with
    nothing logged."""
    conn.close()
    try:
        await asyncio.wait_for(conn.wait_closed(), timeout=_QUICK_CMD_TIMEOUT)
    except asyncio.TimeoutError:
        _LOGGER.warning(
            "[QBIT] remux: connection close timed out host=%s — abandoning cleanup wait",
            host,
        )
    except (OSError, asyncssh.Error):
        _LOGGER.debug("[QBIT] remux: error while closing connection", exc_info=True)


async def identify_tracks(conn, mkvmerge_path: str, source_path: str) -> dict | None:
    """Runs mkvmerge -J against source_path on the given (already-open)
    connection. Returns the parsed identify JSON, or None on failure. Logs
    every track's key properties for troubleshooting — codec, existing
    title, language, and the commentary/hearing-impaired flags (which are
    real Matroska properties, but frequently absent depending on how the
    file was originally encoded)."""
    try:
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
            return None

        try:
            identify = json.loads(result.stdout)
        except ValueError:
            _LOGGER.warning(
                "[QBIT] remux: could not parse mkvmerge -J output source=%s",
                source_path,
            )
            return None

        track_summary = [
            {
                "id": t.get("id"),
                "type": t.get("type"),
                "codec": t.get("codec"),
                "track_name": (t.get("properties") or {}).get("track_name"),
                "language": (t.get("properties") or {}).get("language"),
                "language_ietf": (t.get("properties") or {}).get("language_ietf"),
                "flag_commentary": (t.get("properties") or {}).get("flag_commentary"),
                "flag_hearing_impaired": (t.get("properties") or {}).get("flag_hearing_impaired"),
            }
            for t in (identify.get("tracks") or [])
        ]
        _LOGGER.warning("[QBIT] remux: tracks for %s: %s", source_path, track_summary)
        return identify
    except asyncio.TimeoutError:
        _LOGGER.error("[QBIT] remux: identify timed out source=%s", source_path)
        return None
    except (OSError, asyncssh.Error):
        _LOGGER.exception("[QBIT] remux: SSH error during identify source=%s", source_path)
        return None


async def download_subtitle(conn, url: str, temp_dir: str, filename: str, is_windows: bool) -> str | None:
    """Downloads a subtitle directly on the remote host (files are tiny —
    tens of KB) into temp_dir, returning the full remote path, or None on
    failure. Uses curl on both platforms (Windows 10/11/Server 2019+ ships
    a real curl.exe) rather than PowerShell's Invoke-WebRequest, to avoid
    nested-quoting problems running PowerShell inside an SSH/cmd.exe
    command."""
    sep = "\\" if is_windows else "/"
    remote_path = f"{temp_dir.rstrip(sep)}{sep}{filename}"
    curl_bin = "curl.exe" if is_windows else "curl"
    cmd = f'{curl_bin} -sL -o "{remote_path}" "{url}"'
    try:
        result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=_QUICK_CMD_TIMEOUT)
        if result.exit_status != 0:
            _LOGGER.warning(
                "[QBIT] remux: subtitle download failed url=%s exit=%s stderr=%s",
                url, result.exit_status, result.stderr,
            )
            return None
        return remote_path
    except asyncio.TimeoutError:
        _LOGGER.error("[QBIT] remux: subtitle download timed out url=%s", url)
        return None
    except (OSError, asyncssh.Error):
        _LOGGER.exception("[QBIT] remux: subtitle download error url=%s", url)
        return None


async def _delete_remote_file(conn, path: str, is_windows: bool) -> None:
    cmd = f'del /f /q "{path}"' if is_windows else f'rm -f "{path}"'
    try:
        await asyncio.wait_for(conn.run(cmd, check=False), timeout=_QUICK_CMD_TIMEOUT)
    except (asyncio.TimeoutError, OSError, asyncssh.Error):
        _LOGGER.debug("[QBIT] remux: cleanup of temp file failed path=%s", path, exc_info=True)


async def write_remuxed_file(
    conn,
    mkvmerge_path: str,
    source_path: str,
    dest_path: str,
    identify: dict,
    plan: dict,
    video_title: str | None,
    subtitle_fetches: list[dict] | None,
    nas_username: str = "",
    nas_password: str = "",
    is_windows: bool = True,
) -> bool:
    """Always produces an MKV at dest_path — every file goes through
    mkvmerge, regardless of source container or whether any tracks actually
    needed changing, so output format and track naming stay consistent.

    subtitle_fetches: [{"lang": iso639-2 code, "track_name": display name,
    "url": download URL}, ...] for any selected subtitle language OpenSubtitles
    found but that wasn't already embedded — identify/plan/the search itself
    are the caller's responsibility (HA's own aiohttp session handles the
    OpenSubtitles API calls; this function only ever talks to the remote
    host over SSH). Each one is downloaded to a temp location on the remote
    host, muxed in as an extra subtitle input, and the temp file is deleted
    afterward regardless of outcome.

    On Windows, assumes the SSH server's default shell is cmd.exe (Windows
    OpenSSH Server's own default); if that host has been reconfigured to a
    different DefaultShell, the mkdir/del commands here will need adjusting.

    SSH sessions on Windows are network logons and can't use Windows
    Credential Manager (cmdkey), so if the destination is a UNC path and NAS
    credentials are configured, authenticate to that server explicitly via
    `net use \\host\\IPC$` before writing — this works from a network logon
    since the password is passed inline rather than relying on any
    cached/persisted credential. Skipped entirely on Linux, where
    network-share access is a mount-level concern (fstab/credentials file)
    handled as host setup outside the integration."""
    track_names = compute_track_names(identify, plan, video_title)
    sep = "\\" if is_windows else "/"
    downloaded_paths: list[str] = []

    try:
        if is_windows:
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

        dest_dir = dest_path.rsplit(sep, 1)[0] if sep in dest_path else dest_path
        mkdir_cmd = (
            f'if not exist "{dest_dir}" mkdir "{dest_dir}"' if is_windows
            else f'mkdir -p "{dest_dir}"'
        )
        _LOGGER.debug("[QBIT] remux: mkdir command=%s", mkdir_cmd)
        mkdir_result = await asyncio.wait_for(
            conn.run(mkdir_cmd, check=False), timeout=_QUICK_CMD_TIMEOUT,
        )
        _LOGGER.warning(
            "[QBIT] remux: mkdir precheck dir=%s exit=%s stdout=%r stderr=%r",
            dest_dir, mkdir_result.exit_status, mkdir_result.stdout, mkdir_result.stderr,
        )

        extra_subtitles = []
        if subtitle_fetches:
            needs_download = [s for s in subtitle_fetches if not s.get("remote_path")]
            temp_dir = "%TEMP%\\qbit_airdrop_subs" if is_windows else "/tmp/qbit_airdrop_subs"
            if needs_download:
                temp_mkdir_cmd = (
                    f'if not exist "{temp_dir}" mkdir "{temp_dir}"' if is_windows
                    else f'mkdir -p "{temp_dir}"'
                )
                await asyncio.wait_for(conn.run(temp_mkdir_cmd, check=False), timeout=_QUICK_CMD_TIMEOUT)

            for i, sub in enumerate(subtitle_fetches):
                # Already part of the torrent — reference its existing
                # remote path directly, no download or cleanup needed.
                if sub.get("remote_path"):
                    extra_subtitles.append({
                        "lang": sub["lang"],
                        "track_name": sub["track_name"],
                        "path": sub["remote_path"],
                    })
                    continue

                filename = f"sub_{sub['lang']}_{i}.srt"
                remote_path = await download_subtitle(conn, sub["url"], temp_dir, filename, is_windows)
                if remote_path:
                    downloaded_paths.append(remote_path)
                    extra_subtitles.append({
                        "lang": sub["lang"],
                        "track_name": sub["track_name"],
                        "path": remote_path,
                    })
                else:
                    _LOGGER.warning(
                        "[QBIT] remux: could not fetch subtitle lang=%s for source=%s — proceeding without it",
                        sub["lang"], source_path,
                    )

        remux_cmd = build_remux_command(
            mkvmerge_path, source_path, dest_path, plan, track_names, extra_subtitles,
        )
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
            return False

        _LOGGER.debug("[QBIT] remux: wrote %s", dest_path)
        return True
    except asyncio.TimeoutError:
        _LOGGER.error(
            "[QBIT] remux: timed out source=%s — treating as a failure, will retry next pass",
            source_path,
        )
        return False
    except (OSError, asyncssh.Error):
        _LOGGER.exception("[QBIT] remux: SSH error source=%s", source_path)
        return False
    finally:
        for p in downloaded_paths:
            await _delete_remote_file(conn, p, is_windows)
