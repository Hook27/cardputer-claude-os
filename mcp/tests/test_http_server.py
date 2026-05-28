"""Integration tests for the streamable-HTTP transport wiring.

These exercise the real FastMCP app produced by server.build_http_app()
through a Starlette TestClient — no BLE device is touched because we only
hit auth + the MCP handshake + tools/list (never a tool body, which is
what would drive the Bridge).
"""

import json

import pytest
from starlette.testclient import TestClient

import server

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
_H = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


@pytest.fixture(scope="module")
def client():
    # One app + one TestClient for the whole module: the FastMCP
    # streamable-HTTP session manager's lifespan can only run once per
    # `mcp` instance, and `mcp` is a module-level singleton.
    app = server.build_http_app(
        {"tok": "claude-code"}, extra_allowed_hosts=["testserver"]
    )
    with TestClient(app) as c:
        yield c


def _sse_objects(text):
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return out


def test_missing_bearer_is_401(client):
    r = client.post("/mcp", headers=_H, json=_INIT)
    assert r.status_code == 401


def test_wrong_bearer_is_401(client):
    h = {**_H, "authorization": "Bearer WRONG"}
    r = client.post("/mcp", headers=h, json=_INIT)
    assert r.status_code == 401


def test_initialize_and_tools_list_with_bearer(client):
    h = {**_H, "authorization": "Bearer tok"}
    init = client.post("/mcp", headers=h, json=_INIT)
    assert init.status_code == 200, init.text
    sid = init.headers.get("mcp-session-id")
    assert sid
    h2 = {**h, "mcp-session-id": sid}
    client.post(
        "/mcp",
        headers=h2,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    tl = client.post(
        "/mcp",
        headers=h2,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert tl.status_code == 200, tl.text
    names = set()
    for obj in _sse_objects(tl.text):
        for tool in obj.get("result", {}).get("tools", []):
            names.add(tool["name"])
    assert {"notify", "ask", "confirm"} <= names, names
