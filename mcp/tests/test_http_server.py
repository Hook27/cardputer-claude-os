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
    # `mcp` instance, and `mcp` is a module-level singleton. Two tokens
    # so we can assert token -> agent-label mapping on the device banner.
    app = server.build_http_app(
        {"tok": "claude-code", "cloud": "managed-agent"},
        tunnel_domain="abcd1234.tunnel.anthropic.com",
        extra_allowed_hosts=["testserver"],
    )
    with TestClient(app) as c:
        yield c


def _init_session(client, token):
    h = {**_H, "authorization": f"Bearer {token}"}
    init = client.post("/mcp", headers=h, json=_INIT)
    assert init.status_code == 200, init.text
    sid = init.headers["mcp-session-id"]
    h2 = {**h, "mcp-session-id": sid}
    client.post(
        "/mcp",
        headers=h2,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return h2


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


def test_tunnel_host_header_is_not_421(client):
    # The Host a tunneled request actually arrives with. Must NOT be
    # rejected by DNS-rebinding protection (the bug the *.domain entry
    # silently caused). Bearer is valid, host is allowed -> 200.
    h = {
        **_H,
        "authorization": "Bearer cloud",
        "host": "cardputer.abcd1234.tunnel.anthropic.com",
    }
    r = client.post("/mcp", headers=h, json=_INIT)
    assert r.status_code != 421, "tunnel Host rejected by rebinding protection"
    assert r.status_code == 200, r.text


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


def test_notify_threads_agent_label_from_token(client, monkeypatch):
    # Capture what reaches the Bridge instead of touching BLE.
    captured = {}

    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        captured["cmd"] = cmd
        captured["agent"] = agent
        return {"ack": cmd, "ok": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)

    h = _init_session(client, "cloud")  # token "cloud" -> label "managed-agent"
    r = client.post(
        "/mcp",
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "notify", "arguments": {"title": "hi"}},
        },
    )
    assert r.status_code == 200, r.text
    assert captured.get("cmd") == "notify"
    assert captured.get("agent") == "managed-agent", captured


def test_notify_returns_dnd_when_device_suppresses(client, monkeypatch):
    async def fake_send(cmd, payload, rpc_timeout_s=30.0, agent="mcp-client"):
        return {"ack": cmd, "ok": False, "dnd": True}

    monkeypatch.setattr(server.bridge, "send", fake_send)
    h = _init_session(client, "tok")
    r = client.post(
        "/mcp",
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "notify", "arguments": {"title": "hi"}},
        },
    )
    assert r.status_code == 200, r.text
    texts = []
    for obj in _sse_objects(r.text):
        for c in obj.get("result", {}).get("content", []):
            if c.get("type") == "text":
                texts.append(c["text"])
    assert "dnd" in texts, texts
