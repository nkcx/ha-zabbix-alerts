"""Constants for the Zabbix alerts integration."""

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "zabbix_alerts"
LOGGER = logging.getLogger(__package__)

ZABBIX_DOMAIN: Final = "zabbix"
MIN_ZABBIX_INTEGRATION_VERSION: Final = (0, 3, 0)

CONF_ZABBIX_ENTRY_ID: Final = "zabbix_entry_id"
CONF_API_TOKEN: Final = "api_token"
CONF_ALERT_URL: Final = "alert_url"
DATA_OWNED: Final = "owned"

RECONCILE_INTERVAL: Final = timedelta(hours=1)

ISSUE_NAME_CONFLICT: Final = "name_conflict"
ISSUE_PROVISION_FAILED: Final = "provision_failed"
