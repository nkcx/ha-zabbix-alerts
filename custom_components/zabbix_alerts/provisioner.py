"""Create and maintain the Zabbix objects that send alerts to Home Assistant.

Five objects, all owned by this integration (their ids are stored in the config
entry; nothing else in Zabbix is ever modified or deleted):

1. a webhook **media type** that POSTs to Home Assistant's alert webhook with the
   shared secret in a header;
2. a **user role** without frontend, API or action access;
3. a **user group** with read permission on every host group, because Zabbix
   only sends alerts to users who may see the host;
4. a **user** with that role, in that group, whose media is the webhook URL;
5. a **trigger action** that notifies that user on problem, recovery and update.

``reconcile`` compares what exists with what should exist and creates or updates
only what differs. ``remove`` deletes exactly the objects it created.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import logging
import secrets
from typing import Any

from custom_components.zabbix.api import ZabbixApiError, ZabbixAuthError, ZabbixClient

_LOGGER = logging.getLogger(__name__)

MEDIA_TYPE_NAME = "Home Assistant (ha-zabbix-alerts)"
ROLE_NAME = "Home Assistant alerts (ha-zabbix-alerts)"
USER_GROUP_NAME = "Home Assistant alerts"
USERNAME = "ha-zabbix-alerts"
ACTION_NAME = "Home Assistant alerts (ha-zabbix-alerts)"
MANAGED_NOTE = (
    "Managed by the ha-zabbix-alerts Home Assistant integration "
    "(https://github.com/nkcx/ha-zabbix-alerts). Changes are overwritten; "
    "remove the integration to delete it."
)
SECRET_HEADER = "X-Zabbix-Alert-Secret"

# Runs in Zabbix's JavaScript engine (Duktape). `value` holds the parameters.
MEDIA_TYPE_SCRIPT = """\
// ha-zabbix-alerts media type, script version 1.
var params = JSON.parse(value);
if (!params.URL || !params.Secret) {
    throw 'The URL (user media "Send to") and Secret parameters are required.';
}
var request = new HttpRequest();
request.addHeader('Content-Type: application/json');
request.addHeader('X-Zabbix-Alert-Secret: ' + params.Secret);
if (params.HTTPProxy) {
    request.setProxy(params.HTTPProxy);
}
var response = request.post(params.URL, JSON.stringify({
    event_id: params.event_id,
    event_value: params.event_value,
    event_update_status: params.event_update_status,
    trigger_id: params.trigger_id,
    event_nseverity: params.event_nseverity
}));
if (request.getStatus() !== 200) {
    throw 'Home Assistant answered HTTP ' + request.getStatus() + ': ' + response;
}
return 'OK';
"""

MEDIA_TYPE_WEBHOOK = 4
EVENT_SOURCE_TRIGGERS = 0
PERMISSION_READ = 2
GUI_ACCESS_DISABLED = 3
OPERATION_SEND_MESSAGE = 0
USER_TYPE_USER = 1
# The alert user only receives notifications: no frontend, API or actions.
ROLE_RULES = {
    "ui.default_access": "0",
    "modules.default_access": "0",
    "api.access": "0",
    "actions.default_access": "0",
}


def media_type_parameters(secret: str) -> list[dict[str, str]]:
    """Return the media type's parameters."""
    return [
        {"name": "URL", "value": "{ALERT.SENDTO}"},
        {"name": "Secret", "value": secret},
        {"name": "HTTPProxy", "value": ""},
        {"name": "event_id", "value": "{EVENT.ID}"},
        {"name": "event_value", "value": "{EVENT.VALUE}"},
        {"name": "event_update_status", "value": "{EVENT.UPDATE.STATUS}"},
        {"name": "trigger_id", "value": "{TRIGGER.ID}"},
        {"name": "event_nseverity", "value": "{EVENT.NSEVERITY}"},
    ]


MESSAGE_TEMPLATES = [
    {
        "eventsource": EVENT_SOURCE_TRIGGERS,
        "recovery": recovery,
        "subject": f"{label}: {{EVENT.NAME}}",
        "message": "{EVENT.NAME} on {HOST.NAME}",
    }
    for recovery, label in ((0, "Problem"), (1, "Resolved"), (2, "Updated"))
]


class ProvisionError(Exception):
    """Zabbix could not be configured."""


class NameConflictError(ProvisionError):
    """An object this integration didn't create already has the name it needs."""

    def __init__(self, kind: str, name: str) -> None:
        """Initialize the error."""
        super().__init__(f"A {kind} named {name!r} already exists in Zabbix")
        self.kind = kind
        self.name = name


@dataclass(slots=True)
class OwnedObjects:
    """Ids of the objects this integration created."""

    media_type_id: str | None = None
    role_id: str | None = None
    user_group_id: str | None = None
    user_id: str | None = None
    action_id: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OwnedObjects:
        """Create from stored config entry data."""
        return cls(
            media_type_id=data.get("media_type_id"),
            role_id=data.get("role_id"),
            user_group_id=data.get("user_group_id"),
            user_id=data.get("user_id"),
            action_id=data.get("action_id"),
        )

    def as_dict(self) -> dict[str, str | None]:
        """Return data to store in the config entry."""
        return {
            "media_type_id": self.media_type_id,
            "role_id": self.role_id,
            "user_group_id": self.user_group_id,
            "user_id": self.user_id,
            "action_id": self.action_id,
        }


@dataclass(slots=True)
class ReconcileResult:
    """What a reconcile did."""

    owned: OwnedObjects
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)


class Provisioner:
    """Creates, updates and removes the alerting objects in Zabbix."""

    def __init__(self, client: ZabbixClient) -> None:
        """Initialize with a client whose token belongs to a Super admin."""
        self.client = client

    async def _get(
        self, method: str, id_field: str, object_id: str | None, **params: Any
    ) -> dict[str, Any] | None:
        if not object_id:
            return None
        result = await self.client.call(method, {id_field: [object_id], **params})
        return result[0] if result else None

    async def _check_name_free(
        self, method: str, name_field: str, name: str, kind: str
    ) -> None:
        existing = await self.client.call(
            method, {"output": [name_field], "filter": {name_field: [name]}}
        )
        if existing:
            raise NameConflictError(kind, name)

    async def reconcile(
        self, owned: OwnedObjects, url: str, secret: str
    ) -> ReconcileResult:
        """Make Zabbix match the desired configuration."""
        result = ReconcileResult(owned=owned)
        try:
            await self._reconcile_media_type(result, secret)
            await self._reconcile_role(result)
            await self._reconcile_user_group(result)
            await self._reconcile_user(result, url)
            await self._reconcile_action(result)
        except ZabbixAuthError:
            raise
        except ZabbixApiError as err:
            raise ProvisionError(f"{err.method}: {err.data or err.message}") from err
        return result

    async def _reconcile_media_type(self, result: ReconcileResult, secret: str) -> None:
        owned = result.owned
        desired: dict[str, Any] = {
            "name": MEDIA_TYPE_NAME,
            "type": MEDIA_TYPE_WEBHOOK,
            "status": 0,
            "script": MEDIA_TYPE_SCRIPT,
            "timeout": "30s",
            "process_tags": 0,
            "show_event_menu": 0,
            "description": MANAGED_NOTE,
            "parameters": media_type_parameters(secret),
            "message_templates": MESSAGE_TEMPLATES,
        }
        current = await self._get(
            "mediatype.get",
            "mediatypeids",
            owned.media_type_id,
            output=["name", "status", "script", "timeout", "description", "parameters"],
            selectMessageTemplates=["eventsource", "recovery", "subject", "message"],
        )
        if current is None:
            await self._check_name_free(
                "mediatype.get", "name", MEDIA_TYPE_NAME, "media type"
            )
            created = await self.client.call("mediatype.create", desired)
            owned.media_type_id = str(created["mediatypeids"][0])
            result.created.append("media type")
            return
        if (
            current["name"] != desired["name"]
            or str(current["status"]) != "0"
            or current["script"] != desired["script"]
            or current["timeout"] != desired["timeout"]
            or current["description"] != desired["description"]
            or _pairs(current["parameters"], "name", "value")
            != _pairs(desired["parameters"], "name", "value")
            or _templates(current["message_templates"])
            != _templates(desired["message_templates"])
        ):
            await self.client.call(
                "mediatype.update",
                {"mediatypeid": owned.media_type_id, **desired},
            )
            result.updated.append("media type")

    async def _reconcile_user_group(self, result: ReconcileResult) -> None:
        owned = result.owned
        host_groups = await self.client.call("hostgroup.get", {"output": ["groupid"]})
        rights = sorted(str(group["groupid"]) for group in host_groups)
        desired: dict[str, Any] = {
            "name": USER_GROUP_NAME,
            "gui_access": GUI_ACCESS_DISABLED,
            "users_status": 0,
            "hostgroup_rights": [
                {"id": group_id, "permission": PERMISSION_READ} for group_id in rights
            ],
        }
        current = await self._get(
            "usergroup.get",
            "usrgrpids",
            owned.user_group_id,
            output=["name", "gui_access", "users_status"],
            selectHostGroupRights=["id", "permission"],
        )
        if current is None:
            await self._check_name_free(
                "usergroup.get", "name", USER_GROUP_NAME, "user group"
            )
            created = await self.client.call("usergroup.create", desired)
            owned.user_group_id = str(created["usrgrpids"][0])
            result.created.append("user group")
            return
        current_rights = sorted(
            str(right["id"])
            for right in current.get("hostgroup_rights") or ()
            if str(right["permission"]) == str(PERMISSION_READ)
        )
        if (
            current["name"] != USER_GROUP_NAME
            or str(current["gui_access"]) != str(GUI_ACCESS_DISABLED)
            or str(current["users_status"]) != "0"
            or current_rights != rights
            or len(current.get("hostgroup_rights") or ()) != len(rights)
        ):
            await self.client.call(
                "usergroup.update", {"usrgrpid": owned.user_group_id, **desired}
            )
            result.updated.append("user group")

    async def _reconcile_role(self, result: ReconcileResult) -> None:
        owned = result.owned
        desired: dict[str, Any] = {
            "name": ROLE_NAME,
            "type": USER_TYPE_USER,
            "rules": dict(ROLE_RULES),
        }
        current = await self._get(
            "role.get",
            "roleids",
            owned.role_id,
            output=["name", "type"],
            selectRules="extend",
        )
        if current is None:
            await self._check_name_free("role.get", "name", ROLE_NAME, "user role")
            created = await self.client.call("role.create", desired)
            owned.role_id = str(created["roleids"][0])
            result.created.append("user role")
            return
        rules = current.get("rules") or {}
        if (
            current["name"] != ROLE_NAME
            or str(current["type"]) != str(USER_TYPE_USER)
            or any(str(rules.get(key)) != value for key, value in ROLE_RULES.items())
        ):
            await self.client.call("role.update", {"roleid": owned.role_id, **desired})
            result.updated.append("user role")

    async def _reconcile_user(self, result: ReconcileResult, url: str) -> None:
        owned = result.owned
        media = {
            "mediatypeid": owned.media_type_id,
            "sendto": url,
            "active": 0,
            "severity": 63,
            "period": "1-7,00:00-24:00",
        }
        current = await self._get(
            "user.get",
            "userids",
            owned.user_id,
            output=["username", "surname", "roleid"],
            selectUsrgrps=["usrgrpid"],
            selectMedias=["mediatypeid", "sendto", "active", "severity", "period"],
        )
        if current is None:
            await self._check_name_free("user.get", "username", USERNAME, "user")
            created = await self.client.call(
                "user.create",
                {
                    "username": USERNAME,
                    "name": "Home Assistant",
                    "surname": "alerts (ha-zabbix-alerts)",
                    # Never used: the user has no frontend or API access.
                    "passwd": secrets.token_urlsafe(32) + "aA1!",
                    "roleid": owned.role_id,
                    "usrgrps": [{"usrgrpid": owned.user_group_id}],
                    "medias": [media],
                },
            )
            owned.user_id = str(created["userids"][0])
            result.created.append("user")
            return
        current_media = [
            {
                "mediatypeid": str(item["mediatypeid"]),
                "sendto": item["sendto"],
                "active": str(item["active"]),
                "severity": str(item["severity"]),
                "period": item["period"],
            }
            for item in current.get("medias") or ()
        ]
        desired_media = [{key: str(value) for key, value in media.items()}]
        groups = [str(group["usrgrpid"]) for group in current.get("usrgrps") or ()]
        if (
            current_media != desired_media
            or groups != [owned.user_group_id]
            or str(current.get("roleid")) != owned.role_id
        ):
            await self.client.call(
                "user.update",
                {
                    "userid": owned.user_id,
                    "roleid": owned.role_id,
                    "usrgrps": [{"usrgrpid": owned.user_group_id}],
                    "medias": [media],
                },
            )
            result.updated.append("user")

    async def _reconcile_action(self, result: ReconcileResult) -> None:
        owned = result.owned
        operation = _message_operation(owned.media_type_id, owned.user_id)
        desired: dict[str, Any] = {
            "name": ACTION_NAME,
            "status": 0,
            "esc_period": "1h",
            "pause_symptoms": 0,
            "pause_suppressed": 0,
            "notify_if_canceled": 1,
            "filter": {"evaltype": 0, "conditions": []},
            "operations": [{**operation, "esc_step_from": 1, "esc_step_to": 1}],
            "recovery_operations": [operation],
            "update_operations": [operation],
        }
        current = await self._get(
            "action.get",
            "actionids",
            owned.action_id,
            output=["name", "status", "pause_symptoms", "pause_suppressed"],
            selectFilter=["conditions"],
            selectOperations="extend",
            selectRecoveryOperations="extend",
            selectUpdateOperations="extend",
        )
        if current is None:
            await self._check_name_free("action.get", "name", ACTION_NAME, "action")
            created = await self.client.call(
                "action.create", {"eventsource": EVENT_SOURCE_TRIGGERS, **desired}
            )
            owned.action_id = str(created["actionids"][0])
            result.created.append("action")
            return
        if (
            current["name"] != ACTION_NAME
            or str(current["status"]) != "0"
            or str(current["pause_symptoms"]) != "0"
            or str(current["pause_suppressed"]) != "0"
            or (current.get("filter") or {}).get("conditions")
            or not all(
                _targets(current.get(key) or ())
                == [(owned.media_type_id, owned.user_id)]
                for key in ("operations", "recovery_operations", "update_operations")
            )
        ):
            await self.client.call(
                "action.update", {"actionid": owned.action_id, **desired}
            )
            result.updated.append("action")

    async def remove(self, owned: OwnedObjects) -> list[str]:
        """Delete the objects this integration created; return what was deleted."""
        removed: list[str] = []
        for kind, method, get_method, id_field, object_id in (
            ("action", "action.delete", "action.get", "actionids", owned.action_id),
            ("user", "user.delete", "user.get", "userids", owned.user_id),
            (
                "user group",
                "usergroup.delete",
                "usergroup.get",
                "usrgrpids",
                owned.user_group_id,
            ),
            ("user role", "role.delete", "role.get", "roleids", owned.role_id),
            (
                "media type",
                "mediatype.delete",
                "mediatype.get",
                "mediatypeids",
                owned.media_type_id,
            ),
        ):
            if not object_id:
                continue
            if not await self.client.call(
                get_method, {id_field: [object_id], "output": []}
            ):
                continue
            await self.client.call(method, [object_id])
            removed.append(kind)
        return removed


def _message_operation(
    media_type_id: str | None, user_id: str | None
) -> dict[str, Any]:
    return {
        "operationtype": OPERATION_SEND_MESSAGE,
        "opmessage": {"default_msg": 1, "mediatypeid": media_type_id},
        "opmessage_usr": [{"userid": user_id}],
    }


def _pairs(
    items: Sequence[Mapping[str, Any]], key: str, value: str
) -> list[tuple[str, str]]:
    return sorted((str(item[key]), str(item[value])) for item in items)


def _templates(items: Sequence[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    return sorted(
        (
            str(item["eventsource"]),
            str(item["recovery"]),
            str(item["subject"]),
            str(item["message"]),
        )
        for item in items
    )


def _targets(operations: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for operation in operations:
        message = operation.get("opmessage") or {}
        targets.extend(
            (str(message.get("mediatypeid")), str(user["userid"]))
            for user in operation.get("opmessage_usr") or ()
        )
    return targets
