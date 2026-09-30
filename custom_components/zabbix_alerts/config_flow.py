"""Config flow for the Zabbix alerts integration."""

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.loader import async_get_integration
import voluptuous as vol
from yarl import URL

from custom_components.zabbix.alerts import DATA_ALERT_WEBHOOK_ID, alert_url
from custom_components.zabbix.api import (
    UserType,
    ZabbixAuthError,
    ZabbixError,
)

from . import alerts_ready, client_for
from .const import (
    CONF_ALERT_URL,
    CONF_API_TOKEN,
    CONF_ZABBIX_ENTRY_ID,
    DOMAIN,
    MIN_ZABBIX_INTEGRATION_VERSION,
    ZABBIX_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


def _version(version: str | None) -> tuple[int, ...]:
    parts: list[int] = []
    for part in (version or "0").split(".")[:3]:
        digits = "".join(char for char in part if char.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


async def async_validate_token(
    hass: HomeAssistant, zabbix_entry: ConfigEntry, token: str
) -> str | None:
    """Check the token belongs to a Zabbix Super admin; return an error key."""
    client = client_for(hass, zabbix_entry, token)
    try:
        user = await client.async_get_token_user()
    except ZabbixAuthError:
        return "invalid_auth"
    except ZabbixError:
        _LOGGER.debug("Cannot reach Zabbix", exc_info=True)
        return "cannot_connect"
    if user.type is not UserType.SUPER_ADMIN:
        return "not_super_admin"
    return None


class ZabbixAlertsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zabbix alerts."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._zabbix_entry: ConfigEntry | None = None
        self._token = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the Zabbix server and enter a Super admin token."""
        integration = await async_get_integration(self.hass, ZABBIX_DOMAIN)
        if _version(integration.version) < MIN_ZABBIX_INTEGRATION_VERSION:
            return self.async_abort(
                reason="zabbix_too_old",
                description_placeholders={"version": str(integration.version)},
            )
        entries = self.hass.config_entries.async_entries(ZABBIX_DOMAIN)
        if not entries:
            return self.async_abort(reason="no_zabbix")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            zabbix_entry = self.hass.config_entries.async_get_entry(
                user_input[CONF_ZABBIX_ENTRY_ID]
            )
            assert zabbix_entry is not None
            await self.async_set_unique_id(zabbix_entry.entry_id)
            self._abort_if_unique_id_configured()
            if not alerts_ready(zabbix_entry):
                errors["base"] = "alerts_disabled"
                placeholders["title"] = zabbix_entry.title
            elif error := await async_validate_token(
                self.hass, zabbix_entry, user_input[CONF_API_TOKEN]
            ):
                errors["base"] = error
            else:
                self._zabbix_entry = zabbix_entry
                self._token = user_input[CONF_API_TOKEN]
                return await self.async_step_url()
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ZABBIX_ENTRY_ID, default=entries[0].entry_id
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=entry.entry_id, label=entry.title)
                            for entry in entries
                        ]
                    )
                ),
                vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_url(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the address Zabbix uses to reach Home Assistant."""
        assert self._zabbix_entry is not None
        zabbix_entry = self._zabbix_entry
        default = alert_url(self.hass, zabbix_entry.data[DATA_ALERT_WEBHOOK_ID])
        if user_input is not None:
            return self.async_create_entry(
                title=zabbix_entry.title,
                data={
                    CONF_ZABBIX_ENTRY_ID: zabbix_entry.entry_id,
                    CONF_API_TOKEN: self._token,
                    CONF_ALERT_URL: user_input[CONF_ALERT_URL].strip(),
                },
            )
        return self.async_show_form(
            step_id="url",
            data_schema=vol.Schema(
                {vol.Required(CONF_ALERT_URL, default=default): URL_SELECTOR}
            ),
            description_placeholders={
                "zabbix": URL(zabbix_entry.data["url"]).host or ""
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new Super admin token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            zabbix_entry = self.hass.config_entries.async_get_entry(
                entry.data[CONF_ZABBIX_ENTRY_ID]
            )
            if zabbix_entry is None:
                return self.async_abort(reason="no_zabbix")
            if error := await async_validate_token(
                self.hass, zabbix_entry, user_input[CONF_API_TOKEN]
            ):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address Zabbix uses to reach Home Assistant."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_ALERT_URL: user_input[CONF_ALERT_URL].strip()}
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ALERT_URL, default=entry.data[CONF_ALERT_URL]
                    ): URL_SELECTOR
                }
            ),
        )
