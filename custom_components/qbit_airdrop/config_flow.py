from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
    CONF_BASE_PATH,
    CONF_DOWNLOAD_PATH,
    CONF_MOVIE_PATH,
    CONF_TEMP_HA_PATH,
    CONF_CONFIRM_DELETE,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_MKVMERGE_PATH,
    CONF_NAS_USERNAME,
    CONF_NAS_PASSWORD,
    CONF_RETAIN_LANGUAGES,
    DEFAULT_RETAIN_LANGUAGES,
    CONF_RETAIN_SUBTITLE_LANGUAGES,
    DEFAULT_RETAIN_SUBTITLE_LANGUAGES,
    LANGUAGE_CHOICES,
    CONF_MKVMERGE_HOST_OS,
    DEFAULT_MKVMERGE_HOST_OS,
    MKVMERGE_HOST_OS_CHOICES,
    CONF_OPENSUBTITLES_USERNAME,
    CONF_OPENSUBTITLES_PASSWORD,
    CONF_PAUSE_SUBTITLES_ON_QUOTA,
)
from .util import base_from_data
from . import opensubtitles


def _build_schema(defaults: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Optional(CONF_PORT, default=defaults.get(CONF_PORT, 8080)): int,
        vol.Optional(CONF_BASE_PATH, default=defaults.get(CONF_BASE_PATH, "")): str,
        vol.Optional(CONF_DOWNLOAD_PATH, default=defaults.get(CONF_DOWNLOAD_PATH, "")): str,
        vol.Optional(CONF_MOVIE_PATH, default=defaults.get(CONF_MOVIE_PATH, "")): str,
        vol.Optional(CONF_TEMP_HA_PATH, default=defaults.get(CONF_TEMP_HA_PATH, "")): str,
        vol.Optional(CONF_CONFIRM_DELETE, default=defaults.get(CONF_CONFIRM_DELETE, False)): bool,
        vol.Optional(CONF_SSH_PORT, default=defaults.get(CONF_SSH_PORT, 22)): int,
        vol.Optional(CONF_SSH_USERNAME, default=defaults.get(CONF_SSH_USERNAME, "")): str,
        vol.Optional(CONF_MKVMERGE_PATH, default=defaults.get(CONF_MKVMERGE_PATH, "")): str,
        vol.Optional(
            CONF_MKVMERGE_HOST_OS,
            default=defaults.get(CONF_MKVMERGE_HOST_OS, DEFAULT_MKVMERGE_HOST_OS),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=c["value"], label=c["label"])
                    for c in MKVMERGE_HOST_OS_CHOICES
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_NAS_USERNAME, default=defaults.get(CONF_NAS_USERNAME, "")): str,
        vol.Optional(CONF_NAS_PASSWORD, default=defaults.get(CONF_NAS_PASSWORD, "")): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Optional(
            CONF_RETAIN_LANGUAGES,
            default=defaults.get(CONF_RETAIN_LANGUAGES, DEFAULT_RETAIN_LANGUAGES),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=c["code"], label=c["label"])
                    for c in LANGUAGE_CHOICES
                ],
                multiple=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(
            CONF_RETAIN_SUBTITLE_LANGUAGES,
            default=defaults.get(CONF_RETAIN_SUBTITLE_LANGUAGES, DEFAULT_RETAIN_SUBTITLE_LANGUAGES),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=c["code"], label=c["label"])
                    for c in LANGUAGE_CHOICES
                ],
                multiple=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_OPENSUBTITLES_USERNAME, default=defaults.get(CONF_OPENSUBTITLES_USERNAME, "")): str,
        vol.Optional(
            CONF_OPENSUBTITLES_PASSWORD, default=defaults.get(CONF_OPENSUBTITLES_PASSWORD, "")
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Optional(
            CONF_PAUSE_SUBTITLES_ON_QUOTA,
            default=defaults.get(CONF_PAUSE_SUBTITLES_ON_QUOTA, False),
        ): bool,
    })


def _normalize_input(user_input: dict) -> dict | None:
    host = (user_input.get(CONF_HOST) or "").strip()
    port = user_input.get(CONF_PORT)
    if not host or not isinstance(port, int) or port <= 0:
        return None

    normalized = dict(user_input)
    normalized[CONF_HOST] = host.strip("/")
    normalized[CONF_BASE_PATH] = (user_input.get(CONF_BASE_PATH) or "").strip()
    normalized[CONF_DOWNLOAD_PATH] = (user_input.get(CONF_DOWNLOAD_PATH) or "").strip()
    normalized[CONF_MOVIE_PATH] = (user_input.get(CONF_MOVIE_PATH) or "").strip()
    normalized[CONF_TEMP_HA_PATH] = (user_input.get(CONF_TEMP_HA_PATH) or "").strip()
    normalized[CONF_SSH_USERNAME] = (user_input.get(CONF_SSH_USERNAME) or "").strip()
    normalized[CONF_MKVMERGE_PATH] = (user_input.get(CONF_MKVMERGE_PATH) or "").strip()
    normalized[CONF_MKVMERGE_HOST_OS] = (
        user_input.get(CONF_MKVMERGE_HOST_OS) or DEFAULT_MKVMERGE_HOST_OS
    )
    normalized[CONF_NAS_USERNAME] = (user_input.get(CONF_NAS_USERNAME) or "").strip()
    normalized[CONF_NAS_PASSWORD] = user_input.get(CONF_NAS_PASSWORD) or ""
    normalized[CONF_RETAIN_LANGUAGES] = list(user_input.get(CONF_RETAIN_LANGUAGES) or []) or list(
        DEFAULT_RETAIN_LANGUAGES
    )
    normalized[CONF_RETAIN_SUBTITLE_LANGUAGES] = list(
        user_input.get(CONF_RETAIN_SUBTITLE_LANGUAGES) or []
    ) or list(DEFAULT_RETAIN_SUBTITLE_LANGUAGES)
    normalized[CONF_OPENSUBTITLES_USERNAME] = (user_input.get(CONF_OPENSUBTITLES_USERNAME) or "").strip()
    normalized[CONF_OPENSUBTITLES_PASSWORD] = user_input.get(CONF_OPENSUBTITLES_PASSWORD) or ""
    normalized[CONF_PAUSE_SUBTITLES_ON_QUOTA] = bool(user_input.get(CONF_PAUSE_SUBTITLES_ON_QUOTA, False))
    return normalized


async def _can_login_opensubtitles(hass, data: dict) -> bool:
    """OpenSubtitles is entirely optional — only validated if both fields
    are actually filled in; leaving them blank just disables subtitle
    fetching rather than blocking setup."""
    username = data.get(CONF_OPENSUBTITLES_USERNAME)
    password = data.get(CONF_OPENSUBTITLES_PASSWORD)
    if not (username and password):
        return True

    session = async_get_clientsession(hass)
    token = await opensubtitles.login(session, username, password)
    return token is not None


async def _can_connect(hass, data: dict) -> bool:
    (base,) = base_from_data(data)
    if not base:
        return False

    session = async_get_clientsession(hass)
    try:
        async with session.get(f"{base}/api/v2/app/version", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


class QbitAirdropConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            normalized = _normalize_input(user_input)
            if normalized is None:
                errors["base"] = "invalid_host_port"
            elif not await _can_connect(self.hass, normalized):
                errors["base"] = "cannot_connect"
            elif not await _can_login_opensubtitles(self.hass, normalized):
                errors["base"] = "opensubtitles_login_failed"
            else:
                return self.async_create_entry(title="Qbit Airdrop", data=normalized)

        return self.async_show_form(step_id="user", data_schema=_build_schema({}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return QbitAirdropOptionsFlow(config_entry)


class QbitAirdropOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input=None):
        errors = {}
        defaults = {**self._entry.data, **(self._entry.options or {})}

        if user_input is not None:
            normalized = _normalize_input(user_input)
            if normalized is None:
                errors["base"] = "invalid_host_port"
            elif not await _can_connect(self.hass, normalized):
                errors["base"] = "cannot_connect"
            elif not await _can_login_opensubtitles(self.hass, normalized):
                errors["base"] = "opensubtitles_login_failed"
            else:
                return self.async_create_entry(title="", data=normalized)

        return self.async_show_form(step_id="init", data_schema=_build_schema(defaults), errors=errors)
