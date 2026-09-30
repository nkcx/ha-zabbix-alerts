"""Tests for setting up, maintaining and removing the Zabbix alert setup."""

from datetime import timedelta

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.zabbix.const import CONF_ALERTS
from custom_components.zabbix_alerts.const import DATA_OWNED, DOMAIN
from custom_components.zabbix_alerts.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.zabbix_alerts.provisioner import USER_GROUP_NAME

from .conftest import ALERT_SECRET, ALERT_URL
from .fake_zabbix import ApiFailure, FakeZabbix


async def test_setup_creates_objects(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    assert init_integration.state is ConfigEntryState.LOADED
    owned = init_integration.data[DATA_OWNED]
    assert all(owned.values())
    user = fake_zabbix.store("user")[owned["user_id"]]
    assert user["medias"][0]["sendto"] == ALERT_URL
    media_type = fake_zabbix.store("mediatype")[owned["media_type_id"]]
    parameters = {p["name"]: p["value"] for p in media_type["parameters"]}
    assert parameters["Secret"] == ALERT_SECRET


async def test_unload_keeps_objects(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert len(fake_zabbix.store("action")) == 1


async def test_remove_deletes_objects(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert all(
        not fake_zabbix.store(kind)
        for kind in ("mediatype", "role", "usergroup", "user", "action")
    )


async def test_remove_when_unreachable(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    fake_zabbix.http_status = 503
    assert await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert len(fake_zabbix.store("action")) == 1


async def test_remove_without_zabbix_entry(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    zabbix_entry: MockConfigEntry,
    init_integration: MockConfigEntry,
) -> None:
    await hass.config_entries.async_remove(zabbix_entry.entry_id)
    assert await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert len(fake_zabbix.store("action")) == 1


async def test_setup_waits_for_alerts(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    zabbix_entry: MockConfigEntry,
    config_entry: MockConfigEntry,
) -> None:
    hass.config_entries.async_update_entry(zabbix_entry, options={CONF_ALERTS: False})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_zabbix.writes() == []


async def test_setup_without_zabbix_entry(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, "zabbix_entry_id": "gone"}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failure(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    fake_zabbix.token = "other"
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert [
        flow["context"]["source"] for flow in hass.config_entries.flow.async_progress()
    ] == [SOURCE_REAUTH]


async def test_setup_retries_on_errors(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, config_entry: MockConfigEntry
) -> None:
    fake_zabbix.failures["mediatype.create"] = ApiFailure(-32500, "Error", "Boom")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_name_conflict(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    fake_zabbix.store("usergroup")["9"] = {"usrgrpid": "9", "name": USER_GROUP_NAME}
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    issue = issue_registry.async_get_issue(
        DOMAIN, f"name_conflict_{config_entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders == {
        "kind": "user group",
        "name": USER_GROUP_NAME,
    }
    # The media type created before the conflict is remembered, so it is reused
    # (not duplicated) and deleted on removal.
    media_type_id = config_entry.data[DATA_OWNED]["media_type_id"]
    assert media_type_id in fake_zabbix.store("mediatype")


async def test_periodic_reconcile(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    owned = init_integration.data[DATA_OWNED]
    issue_id = f"provision_failed_{init_integration.entry_id}"

    # Drift is repaired every hour.
    fake_zabbix.host_groups.append("20")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, minutes=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    group = fake_zabbix.store("usergroup")[owned["user_group_id"]]
    assert "20" in {right["id"] for right in group["hostgroup_rights"]}

    # Failures raise a repair issue, cleared by the next successful run.
    fake_zabbix.http_status = 503
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2, minutes=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert issue_registry.async_get_issue(DOMAIN, issue_id)
    assert init_integration.runtime_data.state.last_error is not None
    fake_zabbix.http_status = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=3, minutes=3))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    assert init_integration.runtime_data.state.last_error is None


async def test_periodic_name_conflict(
    hass: HomeAssistant,
    fake_zabbix: FakeZabbix,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    owned = init_integration.data[DATA_OWNED]
    # Our action is deleted and someone creates one with the same name.
    del fake_zabbix.store("action")[owned["action_id"]]
    fake_zabbix.store("action")["55"] = {
        "actionid": "55",
        "name": "Home Assistant alerts (ha-zabbix-alerts)",
    }
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, minutes=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert issue_registry.async_get_issue(
        DOMAIN, f"name_conflict_{init_integration.entry_id}"
    )


async def test_diagnostics(
    hass: HomeAssistant, init_integration: MockConfigEntry, snapshot: SnapshotAssertion
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diagnostics["entry"]["api_token"] == "**REDACTED**"
    assert diagnostics["entry"]["alert_url"] == "**REDACTED**"
    assert diagnostics["created"] == [
        "media type",
        "user role",
        "user group",
        "user",
        "action",
    ]
    assert diagnostics["last_error"] is None
    assert diagnostics["last_run"] is not None


async def test_periodic_auth_failure_starts_reauth(
    hass: HomeAssistant, fake_zabbix: FakeZabbix, init_integration: MockConfigEntry
) -> None:
    fake_zabbix.token = "revoked"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, minutes=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [
        flow["context"]["source"] for flow in hass.config_entries.flow.async_progress()
    ] == [SOURCE_REAUTH]
