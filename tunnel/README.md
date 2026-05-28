# Cardputer over MCP tunnels — reach the device from cloud agents

This directory exposes the local `cardputer-mcp` HTTP daemon to
**cloud Claude** through an [Anthropic MCP tunnel], so a **Managed Agent**
(Console) or a **Messages-API** agent can `notify` / `ask` / `confirm`
on the Cardputer in your pocket — turning the physical hold-Y `confirm`
gesture into a hardware approval key for autonomous cloud agents.

```
 cloud Claude            your Mac (always-on)
 (Managed Agent  ──tunnel──▶  cloudflared ─▶ mcp-proxy ─▶ host:9000  ─BLE─▶ Cardputer
  / Messages API)  outbound-only            (Docker)      cardputer-mcp daemon
```

Outbound-only: no inbound ports, nothing on the public internet. Inner
TLS is terminated by a cert **only you** hold, so Cloudflare can't read
payloads. The tunnel does **not** authenticate to the daemon — that's
what the bearer token is for (see below).

[Anthropic MCP tunnel]: https://platform.claude.com/docs/en/agents-and-tools/mcp-tunnels/overview

## Prerequisites

- **MCP tunnels + Managed Agents beta access** (request at the Console).
  Tunnels are **not** usable from the claude.ai consumer app — only
  Console Managed Agents and the Messages API.
- **Docker Desktop** on the Mac.
- **The HTTP daemon running** on the same Mac (owns the BLE link). Install
  it with [`../mac/install_cardputer_bridge.sh`](../mac/) — it runs
  `mcp/server.py` with `CARDPUTER_HTTP=1` under launchd.
- **The device flashed** with `cardputer_mcp` and powered on (see the
  top-level README).
- `openssl` (ships with macOS).

## One-time setup

1. **Create a tunnel** in the Console → _Manage → MCP tunnels → New_.
   Copy the assigned **domain** (`abcd1234.tunnel.anthropic.com`) and
   reveal the **tunnel token**.

2. **Fill in env:**

   ```bash
   cd tunnel
   cp env.example .env
   $EDITOR .env          # paste TUNNEL_DOMAIN and TUNNEL_TOKEN
   ```

3. **Generate certs** (CA + 90-day server cert; SAN auto-set from
   `TUNNEL_DOMAIN`):

   ```bash
   ./gen-certs.sh
   ```

4. **Register the CA** in the Console: upload `tunnel/data/ca.crt` to your
   tunnel. (A tunnel with no active CA cert won't accept connections or
   appear in the agent's server picker.)

5. **Choose your bearer tokens.** The daemon authenticates every HTTP
   request and maps each token to an agent label shown on the device
   banner. Set them on the daemon via the launchd env (see
   `../mac/install_cardputer_bridge.sh`), e.g.:

   ```
   CARDPUTER_TOKENS=localdevtok=claude-code,cloudtok=managed-agent
   CARDPUTER_TUNNEL_DOMAIN=abcd1234.tunnel.anthropic.com
   ```

   Keep these secret — anyone with a valid token can drive the device.

6. **Start the tunnel stack:**
   ```bash
   docker compose up -d
   docker compose logs -f          # watch for a clean cloudflared connect
   ```

## Use it from a Managed Agent (Console)

In a Managed Agent session, attach the tunnel's MCP server. The URL is
`https://cardputer.<TUNNEL_DOMAIN>/mcp` and the bearer is your cloud
token. The agent now has `notify`, `ask`, `confirm`. Ask it to "confirm a
force-push on the Cardputer" — the device should flash red with
`from: managed-agent`.

## Use it from the Messages API

```bash
curl https://api.anthropic.com/v1/messages \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: mcp-client-2025-11-20" \
  -d '{
    "model": "claude-opus-4-8",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Use the cardputer confirm tool with title DEPLOY prod."}],
    "mcp_servers": [{
      "type": "url",
      "url": "https://cardputer.YOUR_TUNNEL_DOMAIN/mcp",
      "name": "cardputer",
      "authorization_token": "YOUR_CLOUD_TOKEN"
    }],
    "tools": [{"type": "mcp_toolset", "mcp_server_name": "cardputer"}]
  }'
```

## End-to-end verification checklist

| #   | Test                                                                                                                                                                                                 | Expected                                                        |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 1   | **Local loopback:** register the daemon in Claude Code (`claude mcp add --transport http cardputer http://127.0.0.1:9000/mcp --header "Authorization: Bearer localdevtok"`), ask it to confirm an op | red banner `from: claude-code`; hold Y → agent gets `confirmed` |
| 2   | **Cloud over tunnel:** Managed Agent calls `confirm`                                                                                                                                                 | banner shows `from: managed-agent`; hold Y → agent proceeds     |
| 3   | **Messages API:** the curl above                                                                                                                                                                     | device prompts; tool result reflects your gesture               |
| 4   | **Fail-closed (device off):** power the Cardputer off, agent calls `confirm`                                                                                                                         | tool returns `unavailable: …`; agent must STOP, not proceed     |
| 5   | **Fail-closed (daemon down):** `docker compose down` / quit the daemon                                                                                                                               | tunnel route unreachable → agent errors → stops                 |
| 6   | **Auth:** `curl https://cardputer.<domain>/mcp` with no/`wrong` bearer                                                                                                                               | `401 unauthorized`                                              |

## Security notes

- **Treat `TUNNEL_TOKEN` and `data/tls.key` as high-value secrets.** An
  attacker needs _both_ to impersonate your proxy and read payloads.
  `.env` and `data/` are gitignored.
- **Bearer = identity.** Use a distinct token per agent class so the
  device banner truthfully shows who's asking; the label comes from the
  token, not from anything the agent can type.
- **Fail-closed everywhere.** Device off, daemon down, or Mac asleep ⇒
  `confirm` returns `unavailable` and the companion skill instructs the
  agent to stop. An absent safety device is never an approval.
- **Cert rotation.** The server cert is valid 90 days; re-run
  `gen-certs.sh` and (if you regenerated the CA) re-upload `ca.crt`. The
  proxy hot-reloads `tls.crt`/`tls.key`.

## Troubleshooting

- **Agent calls hang / `no route for host` in proxy logs** — `tunnel_domain`
  in `config/mcp-proxy.yaml` must exactly match the assigned domain. If
  the proxy doesn't expand `${TUNNEL_DOMAIN}`, replace it with the literal.
- **`421 Misdirected Request`** — the daemon's host allow-list doesn't
  include the tunnel domain. Set `CARDPUTER_TUNNEL_DOMAIN` on the daemon
  (it's added to `TransportSecuritySettings.allowed_hosts`).
- **Proxy can't reach the daemon** — confirm the daemon is up
  (`curl -i http://127.0.0.1:9000/mcp` → 401 means it's listening) and
  that `192.168.0.0/16` is in `upstream.allowed_ips` (Docker Desktop's
  `host.docker.internal` gateway).
- **`permission denied` reading the key** — keys are `0600` (owner-only);
  on Docker Desktop macOS the proxy (UID 65532) can still read the
  bind-mounted key. On native Linux Docker, use a named volume or
  `chmod 640 data/tls.key` with a shared group — never `0644`.
- **`curl https://…:8080` says "wrong version number"** — expected; the
  proxy listener is plain WebSocket. Verify only via a Managed Agent or
  the Messages API.
