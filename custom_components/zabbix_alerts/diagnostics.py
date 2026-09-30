"""Diagnostics for the Zabbix alerts integration."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ZabbixAlertsConfigEntry, diagnostics_data
from .const import CONF_ALERT_URL, CONF_API_TOKEN

TO_REDACT = {CONF_API_TOKEN, CONF_ALERT_URL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ZabbixAlertsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        **diagnostics_data(entry.runtime_data),
    }
