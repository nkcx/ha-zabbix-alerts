"""A small fake Zabbix JSON-RPC API for the provisioning calls."""

import copy
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

TOKEN = "s" * 64

JsonObject = dict[str, Any]

# method prefix -> (store name, id field, id list param)
OBJECTS = {
    "mediatype": ("mediatypes", "mediatypeid", "mediatypeids"),
    "usergroup": ("usergroups", "usrgrpid", "usrgrpids"),
    "user": ("users", "userid", "userids"),
    "role": ("roles", "roleid", "roleids"),
    "action": ("actions", "actionid", "actionids"),
}
COMMON_GET_PARAMS = {"output", "filter"}
# Real Zabbix rejects unknown parameters; list the ones each get method accepts.
ALLOWED_GET_PARAMS = {
    "mediatype": COMMON_GET_PARAMS | {"mediatypeids", "selectMessageTemplates"},
    "usergroup": COMMON_GET_PARAMS | {"usrgrpids", "selectHostGroupRights"},
    "user": COMMON_GET_PARAMS | {"userids", "selectUsrgrps", "selectMedias"},
    "role": COMMON_GET_PARAMS | {"roleids", "selectRules"},
    "action": COMMON_GET_PARAMS
    | {
        "actionids",
        "selectFilter",
        "selectOperations",
        "selectRecoveryOperations",
        "selectUpdateOperations",
    },
}
NAME_FIELD = {
    "mediatype": "name",
    "usergroup": "name",
    "user": "username",
    "action": "name",
    "role": "name",
}


class ApiFailure(Exception):
    """Raised by handlers to return a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: str) -> None:
        """Initialize."""
        super().__init__(data)
        self.error = {"code": code, "message": message, "data": data}


@dataclass
class FakeZabbix:
    """State and behaviour of the fake server."""

    token: str = TOKEN
    user_type: int = 3
    url: str = ""
    stores: dict[str, dict[str, JsonObject]] = field(
        default_factory=lambda: {store: {} for store, _, _ in OBJECTS.values()}
    )
    host_groups: list[str] = field(default_factory=lambda: ["2", "4", "7"])
    calls: list[tuple[str, Any]] = field(default_factory=list)
    failures: dict[str, ApiFailure] = field(default_factory=dict)
    http_status: int | None = None
    next_id: int = 100

    def method_calls(self, method: str) -> list[Any]:
        """Return the params of every call to ``method``."""
        return [params for name, params in self.calls if name == method]

    def writes(self) -> list[str]:
        """Return the write methods called."""
        return [
            name
            for name, _ in self.calls
            if name.rsplit(".", 1)[-1] in ("create", "update", "delete")
        ]

    def store(self, kind: str) -> dict[str, JsonObject]:
        """Return the objects of a kind."""
        return self.stores[OBJECTS[kind][0]]

    async def handle(self, request: web.Request) -> web.Response:
        """Handle a JSON-RPC request."""
        if self.http_status is not None:
            return web.Response(status=self.http_status)
        body = await request.json()
        method, params = body["method"], body.get("params", {})
        self.calls.append((method, copy.deepcopy(params)))
        try:
            result = self._dispatch(
                method, params, request.headers.get("Authorization")
            )
        except ApiFailure as failure:
            return web.json_response(
                {"jsonrpc": "2.0", "error": failure.error, "id": body["id"]}
            )
        return web.json_response({"jsonrpc": "2.0", "result": result, "id": body["id"]})

    def _dispatch(self, method: str, params: Any, auth: str | None) -> Any:
        if method in self.failures:
            raise self.failures[method]
        if method == "apiinfo.version":
            return "7.4.15"
        if method == "user.checkAuthentication":
            if params.get("token") != self.token:
                raise ApiFailure(-32602, "Invalid params.", "Not authorized.")
            return {"userid": "1", "username": "Admin", "type": self.user_type}
        if auth != f"Bearer {self.token}":
            raise ApiFailure(-32602, "Invalid params.", "Not authorized.")
        if method == "hostgroup.get":
            return [{"groupid": group_id} for group_id in self.host_groups]
        kind, action = method.split(".")
        store_name, id_field, ids_param = OBJECTS[kind]
        store = self.stores[store_name]
        if action == "get":
            unknown = set(params) - ALLOWED_GET_PARAMS[kind]
            if unknown:
                raise ApiFailure(
                    -32602, "Invalid params.", f'unexpected parameter "{min(unknown)}"'
                )
            objects = list(store.values())
            if ids_param in params:
                objects = [o for o in objects if o[id_field] in params[ids_param]]
            for key, values in params.get("filter", {}).items():
                objects = [o for o in objects if o.get(key) in values]
            return copy.deepcopy(objects)
        if action == "create":
            name = params[NAME_FIELD[kind]]
            if any(o.get(NAME_FIELD[kind]) == name for o in store.values()):
                raise ApiFailure(-32602, "Invalid params.", f'"{name}" already exists.')
            self.next_id += 1
            object_id = str(self.next_id)
            store[object_id] = _normalize(
                kind, {**copy.deepcopy(params), id_field: object_id}
            )
            return {f"{id_field}s": [object_id]}
        if action == "update":
            object_id = params[id_field]
            if object_id not in store:
                raise ApiFailure(-32500, "Application error.", "No permissions")
            store[object_id] = _normalize(
                kind, {**store[object_id], **copy.deepcopy(params)}
            )
            return {f"{id_field}s": [object_id]}
        if action == "delete":
            for object_id in params:
                store.pop(object_id, None)
            return {f"{id_field}s": params}
        raise ApiFailure(-32601, "Method not found.", method)


def _normalize(kind: str, data: JsonObject) -> JsonObject:
    """Make stored objects look like Zabbix returns them (strings, defaults)."""
    data = {
        key: str(value)
        if isinstance(value, int) and not isinstance(value, bool)
        else value
        for key, value in data.items()
    }
    if kind == "role":
        data["rules"] = {
            key: str(value) for key, value in data.get("rules", {}).items()
        }
    if kind == "usergroup":
        data["hostgroup_rights"] = [
            {"id": str(right["id"]), "permission": str(right["permission"])}
            for right in data.get("hostgroup_rights", [])
        ]
    if kind == "user":
        data.pop("passwd", None)
        data["medias"] = [
            {key: str(value) for key, value in media.items()}
            for media in data.get("medias", [])
        ]
    if kind == "mediatype":
        data["message_templates"] = [
            {key: str(value) for key, value in template.items()}
            for template in data.get("message_templates", [])
        ]
    if kind == "action":
        data.pop("eventsource", None)
        for key in ("operations", "recovery_operations", "update_operations"):
            data[key] = [
                {
                    "opmessage": {
                        "mediatypeid": str(op["opmessage"]["mediatypeid"]),
                        "default_msg": str(op["opmessage"]["default_msg"]),
                    },
                    "opmessage_usr": [
                        {"userid": str(user["userid"])} for user in op["opmessage_usr"]
                    ],
                }
                for op in data.get(key, [])
            ]
    return data


def make_app(fake: FakeZabbix) -> web.Application:
    """Return an aiohttp app serving the fake API at any */api_jsonrpc.php."""
    app = web.Application()
    app.router.add_post("/{prefix:.*}api_jsonrpc.php", fake.handle)
    return app
