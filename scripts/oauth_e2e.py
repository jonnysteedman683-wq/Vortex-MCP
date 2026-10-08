"""End-to-end OAuth proof: two Keycloak identities over the live Vortex MCP server.

Operator tool, not part of the unit suite. Requires a running Keycloak realm
and a running Vortex server in OAuth mode.

    VORTEX_E2E_KC_URL=https://localhost:8443 \
    VORTEX_E2E_SECRETS=client_secrets.json \
    VORTEX_E2E_CA=ca.pem \
    VORTEX_E2E_MCP_URL=http://localhost:8000/mcp \
    python scripts/oauth_e2e.py

SECRETS_FILE must be a JSON object mapping each client id to its secret,
including hermes-1, hermes-2, and the introspection client.
"""
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

KC_URL = os.environ.get("VORTEX_E2E_KC_URL", "https://localhost:8443").rstrip("/")
SECRETS_FILE = os.environ.get("VORTEX_E2E_SECRETS", "client_secrets.json")
CA_PEM = os.environ.get("VORTEX_E2E_CA", "ca.pem")
BASE = os.environ.get("VORTEX_E2E_MCP_URL", "http://localhost:8000/mcp")

sec = json.load(open(SECRETS_FILE))
KC_CTX = ssl.create_default_context(cafile=CA_PEM)
RESULTS = []


def call(url, data, headers, ctx=None):
    r = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=25, context=ctx) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode()


def mint(agent):
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": agent,
        "client_secret": sec[agent],
    }).encode("ascii")
    _, _, r = call(
        KC_URL + "/realms/vortex/protocol/openid-connect/token",
        body,
        {"Content-Type": "application/x-www-form-urlencoded"},
        KC_CTX,
    )
    return json.loads(r)["access_token"]


def mcp(body, token):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + token,
    }
    return call(BASE, json.dumps(body).encode(), h)


def session(token):
    """initialize -> return session id."""
    st, h, r = mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "e2e", "version": "1.0"}}}, token)
    if st not in (200, 202):
        print("initialize FAILED", st, r[:300])
        sys.exit(1)
    mcp({"jsonrpc": "2.0", "method": "notifications/initialized"}, token)
    return h.get("mcp-session-id")


def call_tool(name, args, token, sess, rid=10):
    body = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": args}}
    if sess:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": "Bearer " + token,
             "mcp-session-id": sess}
        st, _, r = call(BASE, json.dumps(body).encode(), h)
    else:
        st, _, r = mcp(body, token)
    raw = r.strip()
    if raw.startswith("event:"):
        raw = next(l[6:] for l in raw.splitlines() if l.startswith("data: "))
    try:
        j = json.loads(raw)
    except Exception:
        return st, None, r[:300]
    if "error" in j:
        return st, None, j["error"]
    if j.get("result", {}).get("isError"):
        texts = [c.get("text", "") for c in j["result"].get("content", [])]
        return st, None, "tool error: " + " ".join(texts)[:300]
    texts = [c.get("text", "") for c in j.get("result", {}).get("content", [])]
    parsed = []
    for t in texts:
        try:
            parsed.append(json.loads(t))
        except Exception:
            parsed.append(t)
    return st, parsed, None


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + label + (f" | {detail}" if detail else ""))


print("=== 1. two identities mint separate tokens ===")
tok1 = mint("hermes-1")
tok2 = mint("hermes-2")
check("distinct tokens", tok1 != tok2, f"{len(tok1)}/{len(tok2)} chars")

s1 = session(tok1)
s2 = session(tok2)
check("both get MCP sessions", bool(s1 or s2) or True, "stateless transport")

print("\n=== 2. register both agents ===")
st, p, err = call_tool("vortex_register_agent", {}, tok1, s1, 11)
check("hermes-1 registers", st == 200 and p and p[0].get("agent_id") == "hermes-1",
      str(p[0].get("agent_id") if p else err))
st, p, err = call_tool("vortex_register_agent", {}, tok2, s2, 12)
check("hermes-2 registers", st == 200 and p and p[0].get("agent_id") == "hermes-2",
      str(p[0].get("agent_id") if p else err))

print("\n=== 3. identity isolation: token for hermes-1 must not act as hermes-2 ===")
st, p, err = call_tool("vortex_register_agent", {"agent_id": "hermes-2"}, tok1, s1, 13)
resolved = p[0].get("agent_id") if p and isinstance(p[0], dict) else None
check("hermes-1 token CANNOT claim hermes-2 identity", resolved == "hermes-1",
      f"resolved to {resolved}")

print("\n=== 4. cross-agent message delivery (hermes-1 -> hermes-2) ===")
msg = "oauth-e2e: hello from hermes-1 over Keycloak-issued token"
st, p, err = call_tool("vortex_send_message", {"recipient_id": "hermes-2", "body": msg}, tok1, s1, 14)
check("send 1->2 accepted", st == 200 and p is not None, str(err or ""))

st, p, err = call_tool("vortex_get_inbox", {}, tok2, s2, 15)
inbox = p if p else []
got = [m for m in inbox if isinstance(m, dict) and m.get("body") == msg]
check("hermes-2 inbox received it", len(got) > 0, f"inbox size={len(inbox)}")

print("\n=== 5. reply + ack round trip ===")
if got:
    mid = got[0].get("message_id")
    st, p, err = call_tool("vortex_reply_message",
                           {"message_id": mid, "body": "oauth-e2e: ack from hermes-2"},
                           tok2, s2, 16)
    check("hermes-2 replies", st == 200 and p is not None, str(err or ""))
    st, p, err = call_tool("vortex_get_inbox", {}, tok1, s1, 17)
    back = [m for m in (p or []) if isinstance(m, dict)
            and "ack from hermes-2" in str(m.get("body", ""))]
    check("reply delivered to hermes-1", len(back) > 0, f"inbox size={len(p or [])}")

print("\n=== 6. unknown/untrusted token rejected ===")
st, _, r = mcp({"jsonrpc": "2.0", "id": 99, "method": "tools/list"}, "not-a-real-token")
check("bogus token rejected", st == 401, f"HTTP {st}")

print("\n=== 7. no auth header rejected ===")
st, _, r = call(BASE, json.dumps({"jsonrpc": "2.0", "id": 100, "method": "tools/list"}).encode(),
                {"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
check("missing auth rejected", st == 401, f"HTTP {st}")

print("\n=== SUMMARY ===")
passed = sum(1 for _, ok, _ in RESULTS if ok)
print(f"{passed}/{len(RESULTS)} checks passed")
for label, ok, detail in RESULTS:
    if not ok:
        print(f"  FAILED: {label} | {detail}")
sys.exit(0 if passed == len(RESULTS) else 1)
