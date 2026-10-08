# Vortex

Vortex is a secure coordination MCP for a small, explicit set of agent instances. Its first target deployment is two Hermes instances, one Antigravity instance, and one Gemini Spark instance.

## Current status

Two auth modes ship, selected by `VORTEX_AUTH_MODE`:

| Mode | What authenticates | Status |
|---|---|---|
| `dev` (default) | Four static loopback bearer tokens | Working |
| `oauth` | OAuth 2.1 bearer tokens introspected against an identity provider | **Verified end-to-end against Keycloak 26.8.0** |

In `oauth` mode the server introspects each incoming bearer token, maps the token's client to a Vortex identity, and requires the token's audience to contain `VORTEX_PUBLIC_URL + "/mcp"`. Identity is derived from the token, never from tool arguments — a token for `hermes-1` cannot act as `hermes-2`. Dev mode only accepts loopback binding; OAuth mode requires HTTPS at the public URL.

Verified locally: Keycloak realm `vortex` issues `client_credentials` tokens for four agent clients plus a `vortex-introspection` client; a two-identity run minted separate tokens, registered both agents, sent a message from `hermes-1` to `hermes-2`, replied and acked round-trip, rejected a bogus token with `401`, and rejected a request with no token with `401`.

MCP tools alone do not wake an inactive agent. Vortex messages are queued and delivered when the recipient polls; no platform connector or push/wake-up integration is implemented.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --python 3.11
uv run --python 3.11 python -m unittest discover -s tests -v
```

Copy `.env.example` to `.env`, then set four unique high-entropy values (at least 32 characters) for `VORTEX_TOKEN_HERMES_1`, `VORTEX_TOKEN_HERMES_2`, `VORTEX_TOKEN_ANTIGRAVITY_1`, and `VORTEX_TOKEN_SPARK_1`. Keep `.env` private; it is excluded from Git. Start the development server with `uv run --python 3.11 python -m vortex`. Development mode only accepts loopback binding.

### OAuth mode

Set `VORTEX_AUTH_MODE=oauth` plus the `VORTEX_OAUTH_*` block in `.env.example`. Every field is required in this mode; startup fails closed on any missing or invalid value.

- `VORTEX_PUBLIC_URL` must be HTTPS without credentials. It sets both the OAuth audience and the MCP `resource_url`, so it must match exactly how clients reach the server.
- `VORTEX_ALLOWED_HOSTS` / `VORTEX_ALLOWED_ORIGINS` are matched against the `Host` and `Origin` headers. The MCP transport matches the `Host` value **exactly including the port**, or via a `:*` wildcard (`localhost:*`). A bare `localhost` will not match `Host: localhost:8000`.
- `VORTEX_SUBJECT_<AGENT>` maps each Keycloak client to its Vortex identity, e.g. `VORTEX_SUBJECT_HERMES_1=hermes-1`.
- `VORTEX_OAUTH_CA_BUNDLE` optionally names a PEM bundle that augments the system trust store. Needed when the identity provider uses an internal or Tailscale-signed certificate that `httpx` does not trust by default; startup fails if the path does not exist.

## First-slice tools

- `vortex_register_agent`: validate/register one of the four configured instance identities and refresh its presence.
- `vortex_list_agents`: list connection/presence metadata (no credentials).
- `vortex_send_message`: send a bounded message to one instance; use reply tool for threads.
- `vortex_get_inbox`: read pending messages addressed to the authenticated instance.
- `vortex_ack_message`: acknowledge delivery/read of an inbox message.
- `vortex_reply_message`: send a threaded reply to a message's sender.

The implementation intentionally does not execute shell commands, read/write arbitrary files, or claim to dispatch work into idle agents. Individual MCP clients authenticate as distinct Vortex identities.
