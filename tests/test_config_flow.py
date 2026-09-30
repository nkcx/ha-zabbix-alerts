"""Tests for the Zabbix alerts config flow."""

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zabbix.const import CONF_ALERTS
from custom_components.zabbix_alerts.const import (
    CONF_ALERT_URL,
    CONF_API_TOKEN,
    CONF_ZABBIX_ENTRY_ID,
    DATA_OWNED,
    DOMAIN,
)

from .conftest import WEBHOOK_ID
from .fake_zabbix import TOKEN, FakeZabbix


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_full_flow(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, zabbix_entry: MockConfigEntry
) -> None:
    hass.config.internal_url = "http://192.0.2.5:8123"
    result = await _start(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZABBIX_ENTRY_ID: zabbix_entry.entry_id, CONF_API_TOKEN: TOKEN},
    )
    assert result["step_id"] == "url"
    schema_default = result["data_schema"].schema
    default = next(key.default() for key in schema_default if key == CONF_ALERT_URL)
    assert default == f"http://192.0.2.5:8123/api/webhook/{WEBHOOK_ID}"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ALERT_URL: " http://ha.example.com/api/webhook/x "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == zabbix_entry.title
    assert result["data"] == {
        CONF_ZABBIX_ENTRY_ID: zabbix_entry.entry_id,
        CONF_API_TOKEN: TOKEN,
        CONF_ALERT_URL: "http://ha.example.com/api/webhook/x",
    }
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.unique_id == zabbix_entry.entry_id
    assert all(entry.data[DATA_OWNED].values())


async def test_no_zabbix(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_zabbix"


async def test_zabbix_too_old(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, zabbix_entry: MockConfigEntry
) -> None:
    class Old:
        version = "0.2.0"

    with patch(
        "custom_components.zabbix_alerts.config_flow.async_get_integration",
        return_value=Old(),
    ):
        result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "zabbix_too_old"


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda fake: setattr(fake, "token", "other"), "invalid_auth"),
        (lambda fake: setattr(fake, "user_type", 2), "not_super_admin"),
        (lambda fake: setattr(fake, "http_status", 503), "cannot_connect"),
    ],
)
async def test_token_errors(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    zabbix_entry: MockConfigEntry,
    setup: object,
    error: str,
) -> None:
    setup(fake_zabbix)  # type: ignore[operator]
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZABBIX_ENTRY_ID: zabbix_entry.entry_id, CONF_API_TOKEN: TOKEN},
    )
    assert result["errors"] == {"base": error}


async def test_alerts_disabled(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, zabbix_entry: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(zabbix_entry, options={CONF_ALERTS: False})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZABBIX_ENTRY_ID: zabbix_entry.entry_id, CONF_API_TOKEN: TOKEN},
    )
    assert result["errors"] == {"base": "alerts_disabled"}
    assert result["description_placeholders"] == {"title": zabbix_entry.title}


async def test_already_configured(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZABBIX_ENTRY_ID: init_integration.data[CONF_ZABBIX_ENTRY_ID],
            CONF_API_TOKEN: TOKEN,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    result = await init_integration.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    fake_zabbix.token = "n" * 64
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "n" * 64}
    )
    assert result["reason"] == "reauth_successful"
    assert init_integration.data[CONF_API_TOKEN] == "n" * 64


async def test_reauth_without_zabbix(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    zabbix_entry: MockConfigEntry,
    init_integration: MockConfigEntry,
) -> None:
    result = await init_integration.start_reauth_flow(hass)
    await hass.config_entries.async_remove(zabbix_entry.entry_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN}
    )
    assert result["reason"] == "no_zabbix"


async def test_reconfigure(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    result = await init_integration.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ALERT_URL: "http://new.example.com/api/webhook/y"}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    owned = init_integration.data[DATA_OWNED]
    user = fake_zabbix.store("user")[owned["user_id"]]
    assert user["medias"][0]["sendto"] == "http://new.example.com/api/webhook/y"
