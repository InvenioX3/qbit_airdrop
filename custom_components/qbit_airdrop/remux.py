from __future__ import annotations

import asyncio
import json
import logging

import asyncssh
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION, STORAGE_KEY_SSH_KEY_FMT, LANGUAGE_CHOICES, DEFAULT_RETAIN_LANGUAGES

_LOGGER = logging.getLogger(__name__)

# Each configured language (stored as its ISO 639-2 code) also needs to match
# mkvmerge's newer "language_ietf" property, which reports the ISO 639-1
# (two-letter) form instead — expand every selected code to both forms once,
# so track matching is a simple set-containment check.
_CODE_EXPANSIONS = {c["code"]: {c["code"], c["code2"]} for c in LANGUAGE_CHOICES}

# Fallback display name for a subtitle track when it carries neither a
# commentary nor hearing-impaired flag — those flags are optional Matroska
# metadata a release may simply never have set.
_LANG_LABEL_BY_CODE: dict[str, str] = {}
for _c in LANGUAGE_CHOICES:
    _LANG_LABEL_BY_CODE[_c["code"]] = _c["label"]
    _LANG_LABEL_BY_CODE[_c["code2"]] = _c["label"]

_COMMENTARY_LABEL = "Commentary"
_HEARING_IMPAIRED_LABEL = "SDH"


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


def plan_tracks(identify: dict, retain_codes: list[str] | None = None) -> dict:
    """Pure decision logic over mkvmerge -J output.

    Keep subtitles tagged with any of the configured languages, strip the
    rest. Keep only audio tracks tagged with a configured language if any
    exist, otherwise leave every audio track untouched.

    An undefined-language track only triggers a skip when there's no
    configured-language track anywhere in the file (audio or subtitle) —
    that's the only genuinely ambiguous case, where it's unclear whether
    this is content in a language you want with incomplete tags, or truly
    something else. Once any track in the file matches a configured
    language, the file is known to be relevant content, and any other
    undefined track is just treated the same as a known non-matching one —
    dropped, no further ambiguity.

    Returns {"skip": bool, "audio_keep_ids": list[int] | None, "subtitle_keep_ids": list[int]}.
    audio_keep_ids of None means "keep everything" (the --audio-tracks flag
    is omitted rather than passed).
    """
    retained = _expand_retain_codes(retain_codes or DEFAULT_RETAIN_LANGUAGES)

    tracks = identify.get("tracks") or []
    audio = [t for t in tracks if t.get("type") == "audio"]
    subtitles = [t for t in tracks if t.get("type") == "subtitles"]

    retained_audio_ids = [t["id"] for t in audio if _track_lang(t) in retained]
    retained_subtitle_ids = [t["id"] for t in subtitles if _track_lang(t) in retained]

    if not retained_audio_ids and not retained_subtitle_ids:
        # No confirmed configured-language track anywhere. If anything's
        # language is genuinely unknown, don't guess — skip. Otherwise
        # everything is confidently tagged as some other known language:
        # leave audio untouched (nothing configured to prefer) and drop
        # subtitles (no configured-language ones to keep) — the
        # Korean/Chinese-style case.
        if any(_is_undefined(_track_lang(t)) for t in audio + subtitles):
            return {"skip": True, "audio_keep_ids": None, "subtitle_keep_ids": []}
        return {"skip": False, "audio_keep_ids": None, "subtitle_keep_ids": []}

    return {
        "skip": False,
        "audio_keep_ids": retained_audio_ids or None,
        "subtitle_keep_ids": retained_subtitle_ids,
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
    """Track ID -> desired --track-name value.

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


def build_remux_command(
    mkvmerge_path: str, source_path: str, dest_path: str, plan: dict,
    track_names: dict[int, str] | None = None,
) -> str:
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
    return " ".join(parts)


def _unc_host(path: str) -> str:
    if not path.startswith("\\\\"):
        return ""
    return path.lstrip("\\").split("\\", 1)[0]


def build_copy_command(source_path: str, dest_path: str, is_windows: bool) -> str:
    """A skipped (undefined-language) file still needs to reach its real
    destination — just as an unmodified copy rather than a remux, since we
    can't safely decide which tracks to strip."""
    if is_windows:
        return f'copy /Y "{source_path}" "{dest_path}"'
    return f'cp -f "{source_path}" "{dest_path}"'


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


async def remux_file(
    conn,
    mkvmerge_path: str,
    source_path: str,
    dest_path: str,
    nas_username: str = "",
    nas_password: str = "",
    retain_languages: list[str] | None = None,
    is_windows: bool = True,
    video_title: str | None = None,
) -> tuple[bool, bool]:
    """Runs one file's worth of work — identify, decide, then either remux
    or (if skipped for undefined-language tracks) copy as-is — on an
    already-open connection the caller owns (opened once via
    open_connection and reused across every file in a torrent, closed once
    via close_connection when done). Returns (success, skipped) — skipped
    can pair with either outcome: True/True means the as-is copy landed at
    the destination (still tagged for manual review), True/False means a
    normal remux succeeded, and False with either skipped value means the
    write failed and should be retried.

    On Windows, assumes the SSH server's default shell is cmd.exe (Windows
    OpenSSH Server's own default) for the mkdir precheck; if that host has
    been reconfigured to a different DefaultShell, that command will need
    adjusting.

    SSH sessions on Windows are network logons and can't use Windows
    Credential Manager (cmdkey), so if the destination is a UNC path and
    NAS credentials are configured, authenticate to that server explicitly
    via `net use \\host\\IPC$` before writing — this works from a network
    logon since the password is passed inline rather than relying on any
    cached/persisted credential. This whole step is Windows-specific and
    skipped entirely on Linux, where network-share access is a mount-level
    concern (fstab/credentials file) handled as host setup outside the
    integration, not something SSH needs to negotiate per session."""
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
                "codec": t.get("codec"),
                "track_name": (t.get("properties") or {}).get("track_name"),
                "language": (t.get("properties") or {}).get("language"),
                "language_ietf": (t.get("properties") or {}).get("language_ietf"),
                "flag_commentary": (t.get("properties") or {}).get("flag_commentary"),
                "flag_hearing_impaired": (t.get("properties") or {}).get("flag_hearing_impaired"),
            }
            for t in (identify.get("tracks") or [])
        ]
        _LOGGER.warning(
            "[QBIT] remux: tracks for %s: %s",
            source_path, track_summary,
        )

        plan = plan_tracks(identify, retain_languages)
        track_names = compute_track_names(identify, plan, video_title)
        _LOGGER.debug("[QBIT] remux: track_names=%s", track_names)

        sep = "\\" if is_windows else "/"

        # Destination prep (NAS auth + mkdir) happens regardless of the
        # skip decision below — a skipped file still needs to land at its
        # real destination, just as an unmodified copy instead of a remux,
        # so it needs the same directory to exist first.
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

        if plan["skip"]:
            _LOGGER.warning(
                "[QBIT] remux: undefined-language track present — copying as-is instead source=%s",
                source_path,
            )
            copy_cmd = build_copy_command(source_path, dest_path, is_windows)
            _LOGGER.debug("[QBIT] remux: copy command=%s", copy_cmd)
            copy_result = await asyncio.wait_for(
                conn.run(copy_cmd, check=False), timeout=_REMUX_TIMEOUT,
            )
            if copy_result.exit_status != 0:
                _LOGGER.warning(
                    "[QBIT] remux: as-is copy failed source=%s exit=%s stderr=%s",
                    source_path, copy_result.exit_status, copy_result.stderr,
                )
                return False, True
            _LOGGER.debug("[QBIT] remux: copied as-is to %s", dest_path)
            return True, True

        remux_cmd = build_remux_command(mkvmerge_path, source_path, dest_path, plan, track_names)
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
            "[QBIT] remux: timed out source=%s — treating as a failure, will retry next pass",
            source_path,
        )
        return False, False
    except (OSError, asyncssh.Error):
        _LOGGER.exception("[QBIT] remux: SSH error source=%s", source_path)
        return False, False
