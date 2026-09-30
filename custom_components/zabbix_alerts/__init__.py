"""Set up Zabbix to send real-time problem alerts to Home Assistant.

A companion to the Zabbix (ha-zabbix) integration: ha-zabbix receives the alerts
(Configure → Alerts); this integration creates and maintains the Zabbix side.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from custom_components.zabbix.alerts import DATA_ALERT_SECRET, DATA_ALERT_WEBHOOK_ID
from custom_components.zabbix.api import (
    ZabbixAuthError,
    ZabbixClient,
    ZabbixError,
)
from custom_components.zabbix.const import CONF_ALERTS

from .const import (
    CONF_ALERT_URL,
    CONF_API_TOKEN,
    CONF_ZABBIX_ENTRY_ID,
    DATA_OWNED,
    DOMAIN,
    ISSUE_NAME_CONFLICT,
    ISSUE_PROVISION_FAILED,
    LOGGER,
    RECONCILE_INTERVAL,
)
from .provisioner import (
    NameConflictError,
    OwnedObjects,
    Provisioner,
    ProvisionError,
)

type ZabbixAlertsConfigEntry = ConfigEntry[AlertsManager]


@dataclass(slots=True)
class ReconcileState:
    """The outcome of the last reconcile, for diagnostics."""

    last_run: datetime | None = None
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    last_error: str | None = None


class AlertsManager:
    """Keeps the Zabbix alerting objects in sync with Home Assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ZabbixAlertsConfigEntry,
        zabbix_entry: ConfigEntry,
    ) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self.zabbix_entry = zabbix_entry
        self.client = client_for(hass, zabbix_entry, entry.data[CONF_API_TOKEN])
        self.provisioner = Provisioner(self.client)
        self.state = ReconcileState()

    @property
    def owned(self) -> OwnedObjects:
        """Return the objects this integration created."""
        return OwnedObjects.from_dict(self.entry.data.get(DATA_OWNED, {}))

    async def async_reconcile(self) -> None:
        """Make Zabbix match the desired configuration; raises on failure."""
        owned = self.owned
        try:
            result = await self.provisioner.reconcile(
                owned,
                self.entry.data[CONF_ALERT_URL],
                self.zabbix_entry.data[DATA_ALERT_SECRET],
            )
        finally:
            # Keep ids of objects created before a failure, so nothing is orphaned.
            self._store(owned)
        self.state.last_run = dt_util.utcnow()
        self.state.created = result.created
        self.state.updated = result.updated
        self.state.last_error = None
        if result.created or result.updated:
            LOGGER.info(
                "Zabbix alert setup: created %s, updated %s",
                ", ".join(result.created) or "nothing",
                ", ".join(result.updated) or "nothing",
            )

    @callback
    def _store(self, owned: OwnedObjects) -> None:
        if owned.as_dict() != self.entry.data.get(DATA_OWNED):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, DATA_OWNED: owned.as_dict()}
            )

    async def async_periodic_reconcile(self, _now: datetime | None = None) -> None:
        """Reconcile, reporting problems as repair issues instead of raising."""
        entry_id = self.entry.entry_id
        try:
            await self.async_reconcile()
        except ZabbixAuthError as err:
            self.state.last_error = str(err)
            self.entry.async_start_reauth(self.hass)
            return
        except NameConflictError as err:
            self.state.last_error = str(err)
            _create_issue(
                self.hass,
                f"{ISSUE_NAME_CONFLICT}_{entry_id}",
                ISSUE_NAME_CONFLICT,
                {"kind": err.kind, "name": err.name},
            )
            return
        except (ProvisionError, ZabbixError) as err:
            self.state.last_error = str(err)
            LOGGER.warning("Updating the Zabbix alert setup failed: %s", err)
            _create_issue(
                self.hass,
                f"{ISSUE_PROVISION_FAILED}_{entry_id}",
                ISSUE_PROVISION_FAILED,
                {"error": str(err)},
            )
            return
        for issue in (ISSUE_NAME_CONFLICT, ISSUE_PROVISION_FAILED):
            ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{entry_id}")


def client_for(
    hass: HomeAssistant, zabbix_entry: ConfigEntry, token: str
) -> ZabbixClient:
    """Return a client for the Zabbix server of an ha-zabbix entry."""
    return ZabbixClient(
        zabbix_entry.data[CONF_URL],
        token,
        async_get_clientsession(hass, verify_ssl=zabbix_entry.data[CONF_VERIFY_SSL]),
    )


def alerts_ready(zabbix_entry: ConfigEntry) -> bool:
    """Return True if ha-zabbix has its alert receiver turned on."""
    return bool(
        zabbix_entry.options.get(CONF_ALERTS)
        and zabbix_entry.data.get(DATA_ALERT_WEBHOOK_ID)
        and zabbix_entry.data.get(DATA_ALERT_SECRET)
    )


def _create_issue(
    hass: HomeAssistant, issue_id: str, key: str, placeholders: dict[str, str]
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=key,
        translation_placeholders=placeholders,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ZabbixAlertsConfigEntry
) -> bool:
    """Set up the Zabbix alert configuration."""
    zabbix_entry = hass.config_entries.async_get_entry(entry.data[CONF_ZABBIX_ENTRY_ID])
    if zabbix_entry is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="zabbix_entry_missing"
        )
    if not alerts_ready(zabbix_entry):
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="alerts_disabled",
            translation_placeholders={"title": zabbix_entry.title},
        )
    manager = AlertsManager(hass, entry, zabbix_entry)
    try:
        await manager.async_reconcile()
    except ZabbixAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except NameConflictError as err:
        await manager.async_periodic_reconcile()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="name_conflict",
            translation_placeholders={"kind": err.kind, "name": err.name},
        ) from err
    except (ProvisionError, ZabbixError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="provision_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    entry.runtime_data = manager
    entry.async_on_unload(
        async_track_time_interval(
            hass, manager.async_periodic_reconcile, RECONCILE_INTERVAL
        )
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ZabbixAlertsConfigEntry
) -> bool:
    """Unload; the Zabbix objects stay until the integration is removed."""
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: ZabbixAlertsConfigEntry
) -> None:
    """Delete the Zabbix objects this integration created."""
    owned = OwnedObjects.from_dict(entry.data.get(DATA_OWNED, {}))
    zabbix_entry = hass.config_entries.async_get_entry(entry.data[CONF_ZABBIX_ENTRY_ID])
    if zabbix_entry is None:
        LOGGER.warning(
            "The Zabbix integration entry is gone; delete these Zabbix objects by "
            "hand: %s",
            {key: value for key, value in owned.as_dict().items() if value},
        )
        return
    provisioner = Provisioner(
        client_for(hass, zabbix_entry, entry.data[CONF_API_TOKEN])
    )
    try:
        removed = await provisioner.remove(owned)
    except ZabbixError as err:
        LOGGER.warning(
            "Could not delete the Zabbix alert objects (%s); delete them by hand: %s",
            err,
            {key: value for key, value in owned.as_dict().items() if value},
        )
        return
    LOGGER.info("Deleted Zabbix alert objects: %s", ", ".join(removed) or "none")


def diagnostics_data(manager: AlertsManager) -> dict[str, Any]:
    """Return the manager's state for diagnostics."""
    state = manager.state
    return {
        "zabbix_entry": manager.zabbix_entry.title,
        "owned": manager.owned.as_dict(),
        "last_run": state.last_run.isoformat() if state.last_run else None,
        "created": state.created,
        "updated": state.updated,
        "last_error": state.last_error,
    }
