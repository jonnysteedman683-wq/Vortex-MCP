# Vortex

Vortex is a secure coordination MCP for a small, explicit set of agent instances. Its first target deployment is two Hermes instances, one Antigravity instance, and one Gemini Spark instance.

## Current status

The first working slice is a loopback-only Streamable HTTP MCP with a SQLite message store, four configured agent identities, and check-in/inbox/reply tools. **Remote hosting and OAuth 2.1 are not implemented. Do not expose this service to a network.** Keycloak is the recommended provider candidate, but it has not been installed or connected. The OAuth verifier module is a tested integration prototype, not active server authentication.

MCP tools alone do not wake an inactive agent. Vortex messages are queued and delivered when the recipient polls; no platform connector or push/wake-up integration is implemented.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --python 3.11
uv run --python 3.11 python -m unittest discover -s tests -v
```

Copy `.env.example` to `.env`, then set four unique high-entropy values (at least 32 characters) for `VORTEX_TOKEN_HERMES_1`, `VORTEX_TOKEN_HERMES_2`, `VORTEX_TOKEN_ANTIGRAVITY_1`, and `VORTEX_TOKEN_SPARK_1`. Keep `.env` private; it is excluded from Git. Start the development server with `uv run --python 3.11 python -m vortex`. Development mode only accepts loopback binding. Production requires a deployed OAuth 2.1 identity provider, verified client compatibility, HTTPS, and hosted durable storage; none are set up yet.

## First-slice tools

- `vortex_register_agent`: validate/register one of the four configured instance identities and refresh its presence.
- `vortex_list_agents`: list connection/presence metadata (no credentials).
- `vortex_send_message`: send a bounded message to one instance; use reply tool for threads.
- `vortex_get_inbox`: read pending messages addressed to the authenticated instance.
- `vortex_ack_message`: acknowledge delivery/read of an inbox message.
- `vortex_reply_message`: send a threaded reply to a message's sender.

The implementation intentionally does not execute shell commands, read/write arbitrary files, or claim to dispatch work into idle agents. Individual MCP clients authenticate as distinct Vortex identities.
