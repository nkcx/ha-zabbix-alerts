"""Tests for creating and maintaining the Zabbix alert objects."""

from collections.abc import AsyncGenerator

import aiohttp
import pytest

from custom_components.zabbix import alerts as receiver
from custom_components.zabbix.api import ZabbixAuthError, ZabbixClient
from custom_components.zabbix_alerts import provisioner as module
from custom_components.zabbix_alerts.provisioner import (
    ACTION_NAME,
    MEDIA_TYPE_NAME,
    MEDIA_TYPE_SCRIPT,
    USER_GROUP_NAME,
    USERNAME,
    NameConflictError,
    OwnedObjects,
    Provisioner,
    ProvisionError,
)

from .fake_zabbix import TOKEN, ApiFailure, FakeZabbix

URL = "http://ha.example.com:8123/api/webhook/abc"
SECRET = "c" * 64


@pytest.fixture
async def provisioner(fake_zabbix: FakeZabbix) -> AsyncGenerator[Provisioner]:
    """Return a provisioner for the fake server."""
    async with aiohttp.ClientSession() as session:
        yield Provisioner(ZabbixClient(fake_zabbix.url, TOKEN, session))


async def _create(provisioner: Provisioner) -> OwnedObjects:
    owned = OwnedObjects()
    result = await provisioner.reconcile(owned, URL, SECRET)
    assert result.created == ["media type", "user group", "user", "action"]
    return owned


async def test_creates_everything(
    provisioner: Provisioner, fake_zabbix: FakeZabbix
) -> None:
    owned = await _create(provisioner)
    media_type = fake_zabbix.store("mediatype")[owned.media_type_id]
    assert media_type["name"] == MEDIA_TYPE_NAME
    assert media_type["type"] == "4"
    assert media_type["script"] == MEDIA_TYPE_SCRIPT
    parameters = {p["name"]: p["value"] for p in media_type["parameters"]}
    assert parameters["URL"] == "{ALERT.SENDTO}"
    assert parameters["Secret"] == SECRET
    assert parameters["event_id"] == "{EVENT.ID}"
    assert len(media_type["message_templates"]) == 3

    group = fake_zabbix.store("usergroup")[owned.user_group_id]
    assert group["name"] == USER_GROUP_NAME
    assert group["gui_access"] == "3"
    assert {right["id"] for right in group["hostgroup_rights"]} == {"2", "4", "7"}
    assert {right["permission"] for right in group["hostgroup_rights"]} == {"2"}

    user = fake_zabbix.store("user")[owned.user_id]
    assert user["username"] == USERNAME
    assert user["roleid"] == "1"
    assert user["usrgrps"] == [{"usrgrpid": owned.user_group_id}]
    assert user["medias"][0]["sendto"] == URL
    assert user["medias"][0]["mediatypeid"] == owned.media_type_id

    action = fake_zabbix.store("action")[owned.action_id]
    assert action["name"] == ACTION_NAME
    assert action["pause_suppressed"] == "0"
    for key in ("operations", "recovery_operations", "update_operations"):
        assert action[key][0]["opmessage"]["mediatypeid"] == owned.media_type_id
        assert action[key][0]["opmessage_usr"] == [{"userid": owned.user_id}]
    assert fake_zabbix.method_calls("action.create")[0]["eventsource"] == 0


async def test_reconcile_is_idempotent(
    provisioner: Provisioner, fake_zabbix: FakeZabbix
) -> None:
    owned = await _create(provisioner)
    writes = len(fake_zabbix.writes())
    result = await provisioner.reconcile(owned, URL, SECRET)
    assert result.created == []
    assert result.updated == []
    assert len(fake_zabbix.writes()) == writes


async def test_reconcile_repairs_drift(
    provisioner: Provisioner, fake_zabbix: FakeZabbix
) -> None:
    owned = await _create(provisioner)
    # Someone edits the media type and action; a host group is added.
    fake_zabbix.store("mediatype")[owned.media_type_id]["script"] = "return 1;"
    fake_zabbix.store("action")[owned.action_id]["pause_suppressed"] = "1"
    fake_zabbix.host_groups.append("20")
    # The Home Assistant URL and secret change.
    result = await provisioner.reconcile(owned, URL + "2", "d" * 64)
    assert result.updated == ["media type", "user group", "user", "action"]
    media_type = fake_zabbix.store("mediatype")[owned.media_type_id]
    assert media_type["script"] == MEDIA_TYPE_SCRIPT
    assert {p["name"]: p["value"] for p in media_type["parameters"]}[
        "Secret"
    ] == "d" * 64
    group = fake_zabbix.store("usergroup")[owned.user_group_id]
    assert "20" in {right["id"] for right in group["hostgroup_rights"]}
    assert fake_zabbix.store("user")[owned.user_id]["medias"][0]["sendto"] == URL + "2"
    assert fake_zabbix.store("action")[owned.action_id]["pause_suppressed"] == "0"


async def test_reconcile_recreates_deleted_objects(
    provisioner: Provisioner, fake_zabbix: FakeZabbix
) -> None:
    owned = await _create(provisioner)
    old_action = owned.action_id
    del fake_zabbix.store("action")[owned.action_id]
    result = await provisioner.reconcile(owned, URL, SECRET)
    assert result.created == ["action"]
    assert owned.action_id != old_action


async def test_never_takes_over_existing_objects(
    provisioner: Provisioner, fake_zabbix: FakeZabbix
) -> None:
    fake_zabbix.store("usergroup")["9"] = {
        "usrgrpid": "9",
        "name": USER_GROUP_NAME,
        "gui_access": "0",
    }
    owned = OwnedObjects()
    with pytest.raises(NameConflictError) as err:
        await provisioner.reconcile(owned, URL, SECRET)
    assert err.value.kind == "user group"
    # The media type was created before the conflict and is remembered...
    assert owned.media_type_id is not None
    # ...and the existing group was left alone.
    assert fake_zabbix.store("usergroup")["9"]["gui_access"] == "0"
    assert not fake_zabbix.method_calls("usergroup.update")


async def test_api_errors(provisioner: Provisioner, fake_zabbix: FakeZabbix) -> None:
    fake_zabbix.failures["mediatype.create"] = ApiFailure(
        -32500, "Application error.", "No permissions"
    )
    with pytest.raises(ProvisionError, match=r"mediatype\.create"):
        await provisioner.reconcile(OwnedObjects(), URL, SECRET)


async def test_role_fallback(provisioner: Provisioner, fake_zabbix: FakeZabbix) -> None:
    fake_zabbix.roles = [{"roleid": "7", "name": "Custom users", "type": "1"}]
    owned = await _create(provisioner)
    assert fake_zabbix.store("user")[owned.user_id]["roleid"] == "7"
    await provisioner.remove(owned)
    fake_zabbix.roles = []
    with pytest.raises(ProvisionError, match="role"):
        await provisioner.reconcile(OwnedObjects(), URL, SECRET)


async def test_remove(provisioner: Provisioner, fake_zabbix: FakeZabbix) -> None:
    owned = await _create(provisioner)
    unrelated = {"mediatypeid": "5", "name": "Email"}
    fake_zabbix.store("mediatype")["5"] = unrelated
    del fake_zabbix.store("user")[owned.user_id]  # deleted by someone already
    removed = await provisioner.remove(owned)
    assert removed == ["action", "user group", "media type"]
    assert list(fake_zabbix.store("mediatype").values()) == [unrelated]
    assert fake_zabbix.store("usergroup") == {}
    assert fake_zabbix.store("action") == {}
    assert await provisioner.remove(OwnedObjects()) == []


def test_owned_objects_round_trip() -> None:
    owned = OwnedObjects("1", "2", "3", "4")
    assert OwnedObjects.from_dict(owned.as_dict()) == owned
    assert OwnedObjects.from_dict({}) == OwnedObjects()


async def test_auth_errors_pass_through(
    provisioner: Provisioner, fake_zabbix: FakeZabbix
) -> None:
    fake_zabbix.token = "revoked"
    with pytest.raises(ZabbixAuthError):
        await provisioner.reconcile(OwnedObjects(), URL, SECRET)


def test_script_matches_ha_zabbix_receiver() -> None:
    """The webhook script sends the header and fields ha-zabbix's receiver reads."""
    assert module.SECRET_HEADER == receiver.SECRET_HEADER
    assert f"'{receiver.SECRET_HEADER}: '" in module.MEDIA_TYPE_SCRIPT
    for field_name in ("event_id", "event_value", "event_update_status"):
        assert f"{field_name}: params.{field_name}" in module.MEDIA_TYPE_SCRIPT
