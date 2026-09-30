"""Fixtures for Zabbix alerts tests."""

from collections.abc import AsyncGenerator

from aiohttp.test_utils import TestServer
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import (
    HomeAssistantSnapshotExtension,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.zabbix.alerts import DATA_ALERT_SECRET, DATA_ALERT_WEBHOOK_ID
from custom_components.zabbix.api import normalize_url
from custom_components.zabbix.const import CONF_ALERTS
from custom_components.zabbix_alerts.const import (
    CONF_ALERT_URL,
    CONF_API_TOKEN,
    CONF_ZABBIX_ENTRY_ID,
    DOMAIN,
)

from .fake_zabbix import TOKEN, FakeZabbix, make_app

ALERT_SECRET = "c" * 64
WEBHOOK_ID = "b" * 64
ALERT_URL = f"http://ha.example.com:8123/api/webhook/{WEBHOOK_ID}"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom_components."""


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Use the Home Assistant snapshot extension."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
async def fake_zabbix(socket_enabled: None) -> AsyncGenerator[FakeZabbix]:
    """Run a fake Zabbix API on 127.0.0.1."""
    fake = FakeZabbix()
    server = TestServer(make_app(fake), host="127.0.0.1")
    await server.start_server()
    fake.url = str(server.make_url("/zabbix/"))
    yield fake
    await server.close()


@pytest.fixture
def zabbix_entry(hass: HomeAssistant, fake_zabbix: FakeZabbix) -> MockConfigEntry:
    """Return an ha-zabbix entry with alerts turned on.

    It is disabled so the Zabbix integration doesn't poll the fake server; this
    integration only reads the entry's data.
    """
    entry = MockConfigEntry(
        domain="zabbix",
        entry_id="01K6ZABBIXENTRY0000000000",
        title="Zabbix (127.0.0.1)",
        data={
            CONF_URL: normalize_url(fake_zabbix.url),
            "api_token": "r" * 64,
            CONF_VERIFY_SSL: True,
            DATA_ALERT_WEBHOOK_ID: WEBHOOK_ID,
            DATA_ALERT_SECRET: ALERT_SECRET,
        },
        options={CONF_ALERTS: True},
        disabled_by=ConfigEntryDisabler.USER,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def config_entry(zabbix_entry: MockConfigEntry) -> MockConfigEntry:
    """Return a Zabbix alerts entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id="01K6ALERTSENTRY0000000000",
        title=zabbix_entry.title,
        unique_id=zabbix_entry.entry_id,
        data={
            CONF_ZABBIX_ENTRY_ID: zabbix_entry.entry_id,
            CONF_API_TOKEN: TOKEN,
            CONF_ALERT_URL: ALERT_URL,
        },
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
