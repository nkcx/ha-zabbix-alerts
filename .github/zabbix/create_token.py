"""Wait for a fresh Zabbix frontend, then print an API token for Admin."""

import json
import sys
import time
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8080/api_jsonrpc.php"


def call(method: str, params: object, auth: str | None = None) -> object:
    headers = {"Content-Type": "application/json-rpc"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    request = urllib.request.Request(URL, body.encode(), headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


for _ in range(90):
    try:
        session = call("user.login", {"username": "Admin", "password": "zabbix"})
        break
    except (urllib.error.URLError, RuntimeError, ConnectionError, OSError):
        time.sleep(5)
else:
    sys.exit("Zabbix did not come up")

token_id = call("token.create", {"name": "contract", "userid": "1"}, session)[
    "tokenids"
][0]
token = call("token.generate", [token_id], session)[0]["token"]
print(token)
