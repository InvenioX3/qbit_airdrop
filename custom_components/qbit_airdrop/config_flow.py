from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_BASE_PATH, CONF_CONFIRM_DELETE

BASE_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Optional(CONF_PORT, default=8080): int,
    vol.Optional(CONF_BASE_PATH, default=""): str,  # optional
    vol.Optional(CONF_CONFIRM_DELETE, default=False): bool,
})

class QbitAirdropConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            host = (user_input.get(CONF_HOST) or "").strip()
            port = user_input.get(CONF_PORT)
            base_path = (user_input.get(CONF_BASE_PATH) or "").strip()
            if not host or not isinstance(port, int) or port <= 0:
                errors["base"] = "invalid_host_port"
            else:
                # normalize
                user_input[CONF_HOST] = host.strip().strip("/")
                user_input[CONF_BASE_PATH] = base_path
                return self.async_create_entry(title="Qbit Airdrop", data=user_input)

        return self.async_show_form(step_id="user", data_schema=BASE_SCHEMA, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return QbitAirdropOptionsFlow(config_entry)

class QbitAirdropOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input=None):
        errors = {}
        d = {**self._entry.data, **(self._entry.options or {})}
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(d.get(CONF_HOST) or "")): str,
            vol.Optional(CONF_PORT, default=int(d.get(CONF_PORT) or 8080)): int,
            vol.Optional(CONF_BASE_PATH, default=(d.get(CONF_BASE_PATH) or "")): str,
            vol.Optional(CONF_CONFIRM_DELETE, default=bool(d.get(CONF_CONFIRM_DELETE, False))): bool,
        })
        if user_input is not None:
            host = (user_input.get(CONF_HOST) or "").strip()
            port = user_input.get(CONF_PORT)
            base_path = (user_input.get(CONF_BASE_PATH) or "").strip()
            if not host or not isinstance(port, int) or port <= 0:
                errors["base"] = "invalid_host_port"
            else:
                user_input[CONF_HOST] = host.strip().strip("/")
                user_input[CONF_BASE_PATH] = base_path
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
