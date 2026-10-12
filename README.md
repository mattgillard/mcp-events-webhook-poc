# Fastmail → ChatGPT: Day-one MCP Events test

**Scope:** synthetic `email.received` events only. This does not connect to Fastmail or implement a production-ready MCP server. It implements the minimal MCP 2026-07-28 Events JSON-RPC requests directly, *not* using the Python MCP SDK.

## Run locally with uv

Install [uv](https://docs.astral.sh/uv/) if needed, then from this directory:

```bash
uv sync
export TEST_TOKEN="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(24))')"
echo "Your test token: $TEST_TOKEN"
uv run uvicorn server:app --host 127.0.0.1 --port 3000
```

`uv sync` installs the exact versions pinned in `uv.lock`. No manual virtualenv activation is necessary.

In another terminal, test discovery:

```bash
curl -s http://127.0.0.1:3000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}'
```

## Connect from ChatGPT using OpenAI Secure MCP Tunnel (preferred)

This avoids exposing your local `/mcp` service publicly.

1. Open [OpenAI Platform tunnel settings](https://platform.openai.com/settings) and locate **Tunnels**. Create a tunnel and obtain its `tunnel_id`. Associate the Platform organization with your ChatGPT workspace if required.
2. Download the latest [OpenAI tunnel-client](https://github.com/openai/tunnel-client/releases/latest) and check its instructions with `tunnel-client help quickstart`.
3. Set your OpenAI Platform key in your shell as `CONTROL_PLANE_API_KEY` (never commit it).
4. Initialize the HTTP-server profile, substituting your tunnel ID:

```bash
export CONTROL_PLANE_API_KEY="sk-..."
tunnel-client init \
  --profile local-fastmail \
  --tunnel-id tunnel_YOUR_ID \
  --mcp-server-url http://127.0.0.1:3000/mcp

tunnel-client doctor --profile local-fastmail --explain
tunnel-client run --profile local-fastmail
```

5. In ChatGPT select **Plugins → + → Add custom MCP server**, choose **Tunnel** as connection type, and select/paste the tunnel ID. Use **No authentication** only for this synthetic, disposable local test. Start a **Work** chat (Cloud on desktop).

Ask:

> Subscribe to email.received on my test plugin for subject_contains="MCP". When an event arrives, tell me its sender, subject, email ID, and that the event test succeeded. Do not attempt to read Fastmail: these are synthetic test events.

If OpenAI Tunnel isn't available for your workspace, the fallback is `ngrok http 3000` and add the public `https://YOUR-TUNNEL.ngrok.app/mcp` URL instead. Stop either tunnel after testing.

Then POST test event to localhost, replacing the token you exported above:

```bash
curl -s http://127.0.0.1:3000/test \
  -H 'Content-Type: application/json' \
  -H "X-Test-Token: YOUR_TEST_TOKEN" \
  -d '{"email_id":"test-001","from_address":"poc@example.com","subject":"MCP event test"}'
```

**Expected:** callback verification passes during subscription; `/test` returns `matched: 1` with a `2xx` delivery; ChatGPT responds in the subscribed Work chat. A `2xx` means acceptance, not guaranteed final notification delivery.

## Important limitations

- Memory-only subscriptions: process restarts lose the callback/secret.
- No per-user OAuth or authentication on `/mcp`: use only a temporary private test tunnel; disable it after testing.
- Callback URL safety guard is basic, not DNS-pinned; do **not** expose this as a general purpose public webhook forwarder or treat it as production safe.
- No retries, replay, expiration enforcement, or crash durability.
- No actual Fastmail integration; JMAP `EmailDelivery` listener is the next phase.
- Depends on ChatGPT's acceptance of a minimal event-only MCP endpoint; if plugin connection fails due to additional required MCP capabilities, add SDK transport/interop then.

References: https://developers.openai.com/plugins/build/mcp-events and https://developers.openai.com/plugins/build/mcp-server
