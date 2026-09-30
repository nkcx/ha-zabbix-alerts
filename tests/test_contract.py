"""End-to-end test against a real, stock Zabbix server.

Skipped unless ZABBIX_CONTRACT_URL, ZABBIX_CONTRACT_TOKEN (Super admin) and
ZABBIX_CONTRACT_HA_HOST (how the Zabbix server reaches this machine) are set. CI
runs it against the official Zabbix Docker images.
"""

import asyncio
from collections.abc import AsyncGenerator
import os
from pathlib import Path
import time
from typing import Any

import aiohttp
from aiohttp import web
import pytest
from zabbix_utils import AsyncSender

from custom_components.zabbix.api import ZabbixClient
from custom_components.zabbix_alerts.provisioner import (
    SECRET_HEADER,
    OwnedObjects,
    Provisioner,
)

URL = os.environ.get("ZABBIX_CONTRACT_URL", "")
TOKEN = os.environ.get("ZABBIX_CONTRACT_TOKEN", "")
HA_HOST = os.environ.get("ZABBIX_CONTRACT_HA_HOST", "")
SECRET = "e" * 64

pytestmark = pytest.mark.skipif(
    not (URL and TOKEN and HA_HOST), reason="no Zabbix contract server configured"
)


@pytest.fixture
async def client(socket_enabled: None) -> AsyncGenerator[ZabbixClient]:
    """Return a client for the contract server."""
    async with aiohttp.ClientSession() as session:
        yield ZabbixClient(URL, TOKEN, session)


async def _wait_for(check: Any) -> Any:
    async with asyncio.timeout(180):
        while not (result := await check()):  # noqa: ASYNC110 - external server
            await asyncio.sleep(2)
        return result


async def test_media_type_file_imports(client: ZabbixClient) -> None:
    """The manual-setup media type file is a valid Zabbix export."""
    source = Path(__file__).parents[1] / "zabbix" / "media_type_home_assistant.yaml"
    await client.call(
        "configuration.import",
        {
            "format": "yaml",
            "source": source.read_text(encoding="utf-8"),
            "rules": {"mediaTypes": {"createMissing": True, "updateExisting": True}},
        },
    )
    found = await client.call(
        "mediatype.get",
        {"output": ["mediatypeid"], "filter": {"name": ["Home Assistant"]}},
    )
    assert len(found) == 1
    await client.call("mediatype.delete", [found[0]["mediatypeid"]])


async def test_alerts_end_to_end(client: ZabbixClient) -> None:
    """Provision, fire a real problem, receive problem/update/recovery, remove."""
    received: list[tuple[str, dict[str, Any]]] = []

    async def handle(request: web.Request) -> web.Response:
        received.append((request.headers.get(SECRET_HEADER, ""), await request.json()))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/api/webhook/contract", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 18123)
    await site.start()

    provisioner = Provisioner(client)
    owned = OwnedObjects()
    groups = {group.name: group for group in await client.async_get_host_groups()}
    host_name = f"ha-alerts-contract-{int(time.time())}"
    host_id = None
    try:
        webhook_url = f"http://{HA_HOST}:18123/api/webhook/contract"
        result = await provisioner.reconcile(owned, webhook_url, SECRET)
        assert result.created == [
            "media type",
            "user role",
            "user group",
            "user",
            "action",
        ]
        # Real Zabbix returns what was created: a second run changes nothing.
        again = await provisioner.reconcile(owned, webhook_url, SECRET)
        assert (again.created, again.updated) == ([], [])

        created = await client.call(
            "host.create",
            {
                "host": host_name,
                "groups": [{"groupid": groups["Zabbix servers"].group_id}],
            },
        )
        host_id = created["hostids"][0]
        await client.call(
            "item.create",
            {
                "hostid": host_id,
                "name": "Contract value",
                "key_": "contract.value",
                "type": 2,
                "value_type": 3,
                "trapper_hosts": "0.0.0.0/0,::/0",
            },
        )
        await client.call(
            "trigger.create",
            {
                "description": "Contract problem",
                "expression": f"last(/{host_name}/contract.value)>0",
                "priority": 3,
                "manual_close": 1,
            },
        )
        # The host group was created before the user group's rights: reconcile
        # again so the alert user can see the host (as the hourly run would).
        await provisioner.reconcile(owned, webhook_url, SECRET)
        sender = AsyncSender(server="127.0.0.1", port=10051)

        async def problem_sent() -> bool:
            await sender.send_value(host_name, "contract.value", "1")
            return any(body.get("event_value") == "1" for _, body in received)

        await _wait_for(problem_sent)
        secret, problem = next(
            (secret, body)
            for secret, body in received
            if body.get("event_value") == "1"
        )
        assert secret == SECRET
        assert problem["event_id"]
        assert problem["event_nseverity"] == "3"

        await client.call(
            "event.acknowledge",
            {"eventids": [problem["event_id"]], "action": 2 | 4, "message": "contract"},
        )
        await _wait_for(
            lambda: _async_true(
                any(body.get("event_update_status") == "1" for _, body in received)
            )
        )

        async def resolved() -> bool:
            await sender.send_value(host_name, "contract.value", "0")
            return any(
                body.get("event_value") == "0"
                and body.get("event_update_status") == "0"
                for _, body in received
            )

        await _wait_for(resolved)
    finally:
        removed = await provisioner.remove(owned)
        if host_id:
            await client.call("host.delete", [host_id])
        await runner.cleanup()
    assert removed == ["action", "user", "user group", "user role", "media type"]


async def _async_true(value: bool) -> bool:
    return value
