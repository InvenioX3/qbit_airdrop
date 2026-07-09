from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_BASE_PATH, CONF_CONFIRM_DELETE


def _build_schema(defaults: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Optional(CONF_PORT, default=defaults.get(CONF_PORT, 8080)): int,
        vol.Optional(CONF_BASE_PATH, default=defaults.get(CONF_BASE_PATH, "")): str,
        vol.Optional(CONF_CONFIRM_DELETE, default=defaults.get(CONF_CONFIRM_DELETE, False)): bool,
    })


def _normalize_input(user_input: dict) -> dict | None:
    host = (user_input.get(CONF_HOST) or "").strip()
    port = user_input.get(CONF_PORT)
    if not host or not isinstance(port, int) or port <= 0:
        return None

    normalized = dict(user_input)
    normalized[CONF_HOST] = host.strip("/")
    normalized[CONF_BASE_PATH] = (user_input.get(CONF_BASE_PATH) or "").strip()
    return normalized


class QbitAirdropConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            normalized = _normalize_input(user_input)
            if normalized is None:
                errors["base"] = "invalid_host_port"
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
            else:
                return self.async_create_entry(title="", data=normalized)

        return self.async_show_form(step_id="init", data_schema=_build_schema(defaults), errors=errors)
