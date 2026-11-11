# MCP client guide

**Status:** active. **Last updated:** 2026-11-11. **Last verified:** 2026-11-11, against the code at `69a7f84` (error
strings and the `initialize` reply reproduced in process). Not yet checked over a live connection. **Covers:**
`src/api/mcp_server.py`, `src/api/mcp_access.py`, `src/api/mcp_identity.py`, `src/agent/tool_registry.py`,
`src/observability/tool_calls.py`.

> Purpose
>
> How to connect an MCP client to the SRE assistant's `/mcp` endpoint, what the client sees, and what to do when a
> call fails. Written for whoever sets up Claude Code, the claude.ai connector, the nanoclaw assistant or another
> homelab app. Per-tool behaviour lives in [tool-reference.md](tool-reference.md), and the decisions behind the
> endpoint and its authentication live in the [ADR log](adr/README.md).

## 1. What `/mcp` is

`/mcp` is a Streamable HTTP MCP server inside the sre-agent FastAPI app. A client calls the SRE tools directly, with no
LLM in between, so a call costs nothing and usually answers in well under a second.

- **Address:** `https://<sre-mcp-host>/mcp` through the Cloudflare tunnel. Inside the container the app listens on port
  8000. The server rewrites `/mcp` to `/mcp/` itself, so neither form redirects.
- **Server name:** `sre-assistant`. The `initialize` reply carries server instructions: what the tools cover, that
  `memory_record_incident` is the only tool that writes, to call `get_current_time` before building a time range, and
  the accepted time forms.
- **Tools:** up to 36, read-only except `memory_record_incident`. Which ones a client sees depends on the server's
  settings (§6). The deploy capability is never a tool.
- **Stateless:** the server keeps no MCP session between requests, so nothing needs resuming after a restart.
- **Authentication:** Cloudflare Access at the edge (a service token per client), plus an in-app check of the Access
  JWT that also names the client (§5).
- **Observability:** each tool call writes one `tool_call` log line and updates the tool metrics (§9).

## 2. Connect Claude Code

You need a service token's Client ID and Client Secret (§3). Put them and the URL in your shell environment, for
example in a file your shell sources. Never commit them.

```bash
export SRE_MCP_URL='https://<sre-mcp-host>/mcp'
export CF_ACCESS_CLIENT_ID='<client-id>.access'
export CF_ACCESS_CLIENT_SECRET='<client-secret>'
```

### 2.1 With `claude mcp add`

Run:

```bash
claude mcp add --transport http --scope user \
  --header "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  --header "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -- sre-agent "$SRE_MCP_URL"
```

The `--` ends the variadic `--header` list, so keep it. `--scope user` makes the server available in every project.
The shell expands the variables at this point, so the secret is stored in Claude Code's config file
(`~/.claude.json`).

### 2.2 With `.mcp.json`

To keep the secret out of Claude Code's config, put this in `.mcp.json` at the project root. Claude Code expands
`${VAR}` from the environment when it starts the server, and asks you to approve a project server on first use.

```json
{
  "mcpServers": {
    "sre-agent": {
      "type": "http",
      "url": "${SRE_MCP_URL}",
      "headers": {
        "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}",
        "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}"
      }
    }
  }
}
```

### 2.3 Check the connection

1. Run `claude mcp list`. The `sre-agent` line should end in `Connected`.
2. In a new Claude Code session, run `/mcp`. It shows `sre-agent` as connected, with its tools. Claude Code names them
   `mcp__sre-agent__<tool>`, for example `mcp__sre-agent__get_current_time`.
3. Ask Claude to call `get_current_time` on sre-agent. The reply carries the UTC time and the homelab's local time.

If step 1 fails, test the endpoint without Claude Code. This sends an MCP `initialize` request:

```bash
curl -sS "$SRE_MCP_URL" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

A working endpoint answers with one server-sent event (shortened here):

```text
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",...,"serverInfo":{"name":"sre-assistant",...},"instructions":"Read-only SRE tools for a personal Proxmox homelab: ...
```

Anything else is a §10 entry.

## 3. Get a service token

A Cloudflare Access service token is a Client ID and Client Secret pair that a program sends as two headers. Give each
client its own token, so the logs and metrics can tell the clients apart and one token can be revoked alone. These steps
need access to the Cloudflare Zero Trust dashboard.

1. In Zero Trust, open Access, then Service credentials, then Service Tokens, then create a service token. Name it after
   the client, for example `claude-code-laptop`, and pick a duration.
2. Copy the Client ID (it ends in `.access`) and the Client Secret. Cloudflare shows the secret once.
3. On the Access application in front of `<sre-mcp-host>`, make sure a policy with the action **Service Auth** includes
   the token. Without one, Access refuses the token and sends the request to its sign-in page.
4. Name the client for the logs: add `<client-id>.access=<name>` to `MCP_CLIENT_NAMES` in the sre-agent environment,
   then restart sre-agent (§5.1). Until then a verified call from this token shows as `unmapped-service-token`.

To revoke a token, delete it in the same Service Tokens list. Cloudflare refuses its next request (§10.1).

## 4. Other clients

**A client that speaks Streamable HTTP and can set request headers** connects the same way as Claude Code: the URL plus
the `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers. In Python with `fastmcp` (the repo's e2e test does
the same):

```python
import asyncio
import os

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    transport = StreamableHttpTransport(
        os.environ["SRE_MCP_URL"],
        headers={
            "CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"],
            "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"],
        },
    )
    async with Client(transport) as client:
        result = await client.call_tool("get_current_time", {})
        print(result.content[0].text)


asyncio.run(main())
```

**A client that only starts stdio servers** needs a stdio-to-HTTP bridge, such as `mcp-remote`, that forwards the two
headers through its `--header` option. The nanoclaw agent runner's MCP config takes stdio servers (`command`, `args`,
`env`), so it falls in this group.

**The claude.ai connector** takes a server URL and, optionally, OAuth client credentials. It has no field for request
headers, so it can't send a service token, and its requests come from Anthropic's servers. It reaches `/mcp` only if
the Access application admits it another way, which is set in Cloudflare, outside this repository. With the in-app
check in `enforce` mode, that request must also carry an Access JWT for an AUD in `MCP_CF_ACCESS_AUD`. Whether the
claude.ai connector calls this server at all is unconfirmed. The per-client metrics in §9 answer that once
`MCP_CLIENT_NAMES` names the clients.

**A homelab app on the LAN that calls the app's port directly** skips Cloudflare, so its requests carry no Access JWT.
It works while the check is in `off` or `log` mode and shows as `anonymous` (or `unknown`). Under `enforce` it gets a
403. Route it through `<sre-mcp-host>` with a service token instead.

## 5. Authentication modes

Cloudflare Access sits in front of the tunnel route and adds a signed JWT, the `Cf-Access-Jwt-Assertion` header, to
each request it lets through. The app's own check (`McpAccessCheck` in `src/api/mcp_access.py`) runs inside the `/mcp`
app on each HTTP request. It verifies the JWT and names the caller. The JWT must be RS256, signed by one of the team's
Access keys, issued by the team domain, for one of the AUD tags in `MCP_CF_ACCESS_AUD`, and within its `exp` and `iat`
times (60 s leeway). A JWT with an `email` claim names a person. One with a `common_name` claim names a service token
by its Client ID.

### 5.1 Settings

Read once at startup, so a change needs an sre-agent restart.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_ACCESS_MODE` | `log` | `off`, `log` or `enforce` (below). |
| `MCP_CF_ACCESS_AUD` | empty | AUD tag(s) of the Access application in front of `/mcp`, comma-separated. There is no fallback to `CF_ACCESS_AUD`, which belongs to the Upgrade Advisor's approval app. |
| `CF_ACCESS_TEAM_DOMAIN` | empty | The team's bare `<team>.cloudflareaccess.com` hostname. Shared with the approval check. |
| `MCP_CLIENT_NAMES` | empty | `identity=name` pairs, comma-separated. The identity is a service token's Client ID or a person's email (case-insensitive). The name must match `[a-z0-9][a-z0-9_.-]{0,31}`. A malformed pair stops the app at startup. |

At startup the app logs one line with the effective setup, and never a setting's value:

```text
MCP access check: mode=log aud_configured=yes team_domain_configured=yes client_names=2
```

### 5.2 What each mode does

| Mode | No JWT | Invalid JWT | Valid JWT | `access` label |
| --- | --- | --- | --- | --- |
| `off` | passes as `unknown` | passes as `unknown` | passes as `unknown` | `off` |
| `log`, AUD or team domain empty | passes as `unknown` | passes as `unknown` | passes as `unknown` | `unconfigured` |
| `log` | passes as `anonymous` | passes as `invalid` | passes, named | `missing`, `invalid`, `verified` |
| `enforce` | 403 | 403 | passes, named | `missing`, `invalid`, `verified` |
| `enforce`, AUD or team domain empty | 503 | 503 | 503 | `unconfigured` |

- **Named** means the name `MCP_CLIENT_NAMES` gives the identity, else `unmapped-service-token` or `unmapped-user`.
- **A refused or unverified request** writes a WARNING with the peer address and the reason, at most once per peer
  and reason every 10 minutes. An unexpected failure inside the check logs an ERROR, "verification failed (<class>)".
- **`log` with the AUD or team domain empty** warns once at startup ("MCP access check inactive") and checks nothing.
- **`enforce` with the AUD or team domain empty** logs an ERROR at startup ("MCP access check misconfigured") and
  `/mcp` answers 503 while the rest of the app runs.
- **Neither the token, its claims nor the raw identity** reaches a log line or a metric label. A verified call's
  `tool_call` line carries `identity_ref`, the first 8 hex characters of the SHA-256 of the lowercased identity.

### 5.3 From log to enforce

The deployed server runs `log` with no AUD set, so each call counts as `unconfigured` and shows `client=unknown`.
home-server issue #422 tracks the first two steps.

1. Read the AUD tag of the Access application in front of `<sre-mcp-host>` (Zero Trust, Access, Applications, the app's
   overview).
2. Set `MCP_CF_ACCESS_AUD` and `MCP_CLIENT_NAMES`, keep `MCP_ACCESS_MODE=log`, then restart sre-agent. The startup line
   should read `aud_configured=yes team_domain_configured=yes`.
3. Watch for at least 7 days. This query should show only `verified` requests with mapped names:

   ```promql
   sum by (client, access) (increase(sre_assistant_mcp_http_requests_total[1d]))
   ```

   An `anonymous`, `invalid` or `unmapped-*` series is a client to fix or name first.
4. Set `MCP_ACCESS_MODE=enforce`, then restart sre-agent. `enforce` also closes the direct LAN route to `/mcp`.

To roll back, set `MCP_ACCESS_MODE=log` (or `off`), then restart.

## 6. Tools by group and the setting each needs

Each tool appears once in `TOOL_SPECS` (`src/agent/tool_registry.py`) with the setting that enables it. At startup the
server registers the enabled ones, and logs each disabled group once, for example
`proxmox tools disabled on mcp: proxmox_list_guests, proxmox_get_guest_config, proxmox_node_status, proxmox_list_tasks
(PROXMOX_URL not set)`.

| Group | Listed when | Tools |
| --- | --- | --- |
| core (13) | always. `PROMETHEUS_URL`, `GRAFANA_URL` and `GRAFANA_SERVICE_ACCOUNT_TOKEN` are required, so the app does not start without them | `get_current_time`, `prometheus_search_metrics`, `prometheus_instant_query`, `prometheus_range_query`, `scrape_targets`, `container_status`, `grafana_get_alerts`, `grafana_get_alert_rules`, `grafana_get_dashboard`, `grafana_search_dashboards`, `upgrade_status`, `service_health`, `runbook_search` |
| proxmox (4) | `PROXMOX_URL` set | `proxmox_list_guests`, `proxmox_get_guest_config`, `proxmox_node_status`, `proxmox_list_tasks` |
| truenas (6) | `TRUENAS_URL` set (`hdd_power_status` also needs `PROMETHEUS_URL`) | `truenas_pool_status`, `truenas_list_shares`, `truenas_snapshots`, `truenas_system_status`, `truenas_apps`, `hdd_power_status` |
| loki (4) | `LOKI_URL` set | `loki_query_logs`, `loki_metric_query`, `loki_list_label_values`, `loki_correlate_changes` |
| pbs (3) | `PBS_URL` set | `pbs_datastore_status`, `pbs_list_backups`, `pbs_list_tasks` |
| memory (4) | `MEMORY_DB_PATH` set (empty by default) | `memory_search_incidents`, `memory_record_incident` (writes one incident record), `memory_get_previous_report`, `memory_check_baseline` |
| conversations (2) | `CONVERSATION_HISTORY_DIR` set (default `/app/conversations`) | `sre_agent_list_conversations`, `sre_agent_get_conversation`. On `/mcp` only, the chat agent doesn't get them |

To print the list from the code:

```bash
uv run python -c '
from src.agent.tool_registry import TOOL_SPECS
for s in TOOL_SPECS: print(s.group, s.name)
'
```

The deployed server leaves `MEMORY_DB_PATH` empty, so its clients saw 32 tools on 2026-11-11.

## 7. Config-dependent tools

These tools are listed, but what they return depends on a setting or on data outside the server.

| Tool | Depends on | Without it |
| --- | --- | --- |
| `upgrade_status` | `UPGRADE_ADVISOR_DB_PATH` | Prints `Upgrade Advisor not configured (UPGRADE_ADVISOR_DB_PATH is empty): no verdicts or deploy history`, and still shows the exporter's outdated images from Prometheus. |
| `service_health` | `LOKI_URL` | The Loki source reads `unavailable: LOKI_URL not set`. The other four sources still decide the verdict. |
| `runbook_search` | the Chroma store built by `sre-ingest` | Returns "No relevant runbook content found" before the first ingest, and after a re-ingest until sre-agent restarts. `Runbook search failed (...)` means the store can't be read, for example a permission error on its directory. |
| `container_status`, `service_health`, `upgrade_status` | the container-status exporter's metrics in Prometheus | The line for that source reads `unavailable: <reason>` and the rest still prints. |
| `pbs_list_backups` without `datastore` | `PBS_DEFAULT_DATASTORE` | `No datastore specified and PBS_DEFAULT_DATASTORE is not configured. ...` |
| `truenas_*`, and the TrueNAS part of `hdd_power_status` | an `https://` `TRUENAS_URL` | `TrueNAS URL must be https; refusing to send the API key over plain http`. |
| `proxmox_*`, `pbs_*`, `truenas_*` | `PROXMOX_API_TOKEN`, `PBS_API_TOKEN`, `TRUENAS_API_KEY` | The group is listed on its URL alone, so a missing credential shows up as the upstream API's refusal. |

## 8. Limits and errors

**Schemas carry the limits.** `tools/list` gives each parameter its type, default, description and bounds (`minimum`,
`maximum`, `maxLength`, `pattern`, `enum`), and each schema sets `additionalProperties: false`. The server validates a
call against the schema before the tool runs. Results are plain text, with no output schema.

**Limits that the schema doesn't show:**

- Time arguments accept `now`, a duration before now (`1h`, `-1h`, `now-1h`, units `ms s m h d w`, compound forms such
  as `1h30m`), an RFC3339 timestamp (no zone means UTC) or Unix epoch seconds.
- `prometheus_range_query` covers at most 90 days (the Prometheus retention), at most 11,000 points, and a step of at
  most one day. Without `step` it picks one that gives about 250 points per series.
- Most tools give each upstream request 15 s. `truenas_snapshots` allows 30 s to count and 45 s to list.

**Errors.** A failed call returns an MCP result with `isError: true` and one line of text. The HTTP status stays 200.

| Cause | Text the client gets | `outcome` in the log |
| --- | --- | --- |
| A value outside its bounds | `Invalid arguments for loki_query_logs: limit: Input should be less than or equal to 1000` | `invalid_args` |
| An argument name the schema lacks | `Unknown argument(s) for prometheus_search_metrics: since. Accepted: search_term` | `invalid_args` |
| A tool name the server lacks | `Unknown tool: 'no_such_tool'` | `unknown_tool` |
| An upstream timeout | e.g. `Prometheus query timed out after 15s: ...` | `timeout` |
| An upstream error | e.g. `Prometheus API error: HTTP 400 - ...` | `error` |
| A bug in the server | an error result | `internal` (logged at ERROR with a traceback) |

`container_status`, `service_health` and `upgrade_status` read several sources. When one source fails, the call still
succeeds, and that source's line reads `unavailable: <reason>`.

**HTTP-level answers**, before any tool runs:

| Status | Body | Cause |
| --- | --- | --- |
| 403 | `{"error":"Cloudflare Access JWT required"}` | `enforce` mode, no valid Access JWT |
| 503 | `{"error":"MCP access check misconfigured"}` | `enforce` mode with the AUD or team domain empty |
| 503 | `{"error":"MCP server not started"}` | the app is starting or stopping |
| 502 | Cloudflare's page | the app is down, usually during a deploy |

## 9. Seeing your calls

### 9.1 The `tool_call` log line

Each tool call writes one INFO line on the `src.observability.tool_calls` logger. In Loki:

```logql
{service_name="sre-agent"} |= "tool_call"
```

A successful call, as production logs it:

```text
tool_call tool=service_health source=mcp client=unknown outcome=success duration_ms=197 result_chars=738 args="name='immich'"
```

A refused one adds the error:

```text
tool_call tool=loki_query_logs source=mcp client=unknown outcome=invalid_args duration_ms=1 result_chars=0 args="limit=1500 query='{job=\"x\"}'" error_class=InvalidToolArguments error="Invalid arguments for loki_query_logs: limit: Input should be less than or equal to 1000"
```

| Field | Meaning |
| --- | --- |
| `tool` | The tool name as called. |
| `source` | `mcp` for `/mcp`, `agent` for the chat agent's own calls. |
| `client` | The mapped client name, `unmapped-service-token`, `unmapped-user`, `anonymous`, `invalid`, `unknown`, `in-process` (no HTTP request) or `agent`. |
| `outcome` | `success`, `error`, `invalid_args`, `unknown_tool`, `timeout` or `internal`. |
| `duration_ms` | Time spent in the call. |
| `result_chars` | Length of the text returned, 0 on failure. The result itself is never logged. |
| `args` | Arguments, sorted by name. Scalars show their value cut to 80 characters, anything else its type name. At most 400 characters. |
| `identity_ref` | Verified callers only: an 8-character hash of the identity. |
| `error_class`, `error` | Failures only. `error` is cut to 200 characters. |

The line parses with `| logfmt`, so one client's failures are:

```logql
{service_name="sre-agent"} |= "tool_call" | logfmt | client="claude-code" | outcome!="success"
```

`client=unknown` is expected until the AUD is set (§5.3).

### 9.2 Metrics

The app serves Prometheus metrics at `/metrics`. The Grafana dashboard `dashboards/sre-assistant-sli.json` (SRE
Assistant SLI/SLO) plots them by tool, source, client and access result.

| Metric | Labels | What it counts |
| --- | --- | --- |
| `sre_assistant_tool_calls_total` | `tool_name`, `status`, `source`, `client` | Tool calls by outcome. A tool name the server lacks is counted as `tool_name="unknown"`. |
| `sre_assistant_tool_call_duration_seconds` | `tool_name`, `source` | Call duration histogram. |
| `sre_assistant_tool_result_chars` | `tool_name`, `source` | Result size histogram, successful calls only. |
| `sre_assistant_mcp_http_requests_total` | `client`, `access` | HTTP requests to `/mcp`, including `initialize` and `tools/list`, by Access check result. |

Useful queries:

```promql
# Calls per client and tool over a day
sum by (client, tool_name) (increase(sre_assistant_tool_calls_total{source="mcp"}[1d]))

# Tool failures, leaving out the caller's own mistakes
sum by (tool_name, status) (increase(sre_assistant_tool_calls_total{source="mcp", status!~"success|invalid_args|unknown_tool"}[1d]))

# p95 duration per tool
histogram_quantile(0.95, sum by (tool_name, le) (rate(sre_assistant_tool_call_duration_seconds_bucket{source="mcp"}[1h])))
```

## 10. Troubleshooting

### 10.1 `403 Forbidden` from Cloudflare, or a redirect to a `cloudflareaccess.com` sign-in page

Cloudflare Access refused the request before it reached the app. The body is Cloudflare's HTML page. Claude Code
usually reports the server as failed to connect.

1. Check both headers are sent, spelled `CF-Access-Client-Id` and `CF-Access-Client-Secret`, with the values of one
   token. Run the `curl` check in §2.3.
2. Check the token still exists and hasn't expired, in Zero Trust under Service Tokens.
3. Check the Access application for `<sre-mcp-host>` has a **Service Auth** policy that includes the token (§3).
4. If all three hold, read the Access authentication logs in Zero Trust (Logs, Access) for the refused request. They
   name the application and the policy that decided it.

### 10.2 `403` with `{"error":"Cloudflare Access JWT required"}`

The in-app check is in `enforce` mode and the request carried no valid Access JWT. The sre-agent log has a WARNING
with the reason:

```text
MCP access check: request from peer <peer> without a valid Access JWT (<reason>), refused with 403; ...
```

| Reason | Fix |
| --- | --- |
| `missing Cf-Access-Jwt-Assertion header` | The request skipped Cloudflare (a direct LAN call) or the Access application lets it through without a JWT (a Bypass policy). Route it through `<sre-mcp-host>` with a service token. |
| `token rejected (InvalidAudienceError)` | The JWT belongs to another Access application. Add that application's AUD to `MCP_CF_ACCESS_AUD`, or route the client through the right one. |
| `token rejected (ExpiredSignatureError)` | The JWT expired, or a clock is off by more than the 60 s leeway. Retry, then check the host's time sync. |
| `CF_ACCESS_TEAM_DOMAIN is not a bare *.cloudflareaccess.com hostname` | The setting has a scheme, path or other host. Set it to `<team>.cloudflareaccess.com` alone. The startup line still reads `team_domain_configured=yes`, so check the value itself. |
| `unknown key id`, `JWKS fetch failed (...)` | The app could not get the team's signing keys. Check `CF_ACCESS_TEAM_DOMAIN` and that the container can reach `<team>.cloudflareaccess.com`. |

If the cause isn't clear and clients must keep working, set `MCP_ACCESS_MODE=log` and restart. Requests then pass, and
the WARNING and the `access` label still show what failed.

### 10.3 `401`, or Claude Code says the server needs authentication

The in-app check never answers 401, and the server has no OAuth endpoints. A 401 comes from in front of the app, and
Claude Code answers a 401 by offering an OAuth sign-in that can't work here. Treat it as §10.1: check the headers and
the token.

### 10.4 `503` with `{"error":"MCP access check misconfigured"}`

`MCP_ACCESS_MODE=enforce` with `MCP_CF_ACCESS_AUD` or `CF_ACCESS_TEAM_DOMAIN` empty. The startup ERROR names the
missing setting. Set it, or set `MCP_ACCESS_MODE=log`, then restart sre-agent.

### 10.5 `502 Bad gateway` for about 20 seconds

Each deploy recreates the sre-agent container. WUD deploys a new image within minutes of a merge to `main`, and the
tunnel answers 502 until the new process is up, about 20 s. home-server issue #426 tracks it.

1. Wait 30 seconds, then retry. In Claude Code, run `/mcp` and reconnect `sre-agent` if it shows as failed.
2. To confirm a deploy, look for `Started server process` in `{service_name="sre-agent"}` in Loki, or a jump in
   `max(process_start_time_seconds{job="sre-agent"})` in Prometheus.
3. If the 502 lasts more than a minute, the app is down or restarting in a loop. Check the container's logs on its
   host. `/health` answers once the app is up, and its `mcp_server` component should be `healthy`.

### 10.6 `503` with `{"error":"MCP server not started"}`

The app is starting or stopping. Retry after a few seconds. If it persists, `/health` reports `mcp_server` as
`unhealthy` with a detail such as `MCP server not started` or `<n> tools listed, <m> registered`.

### 10.7 A tool is missing from the list

The tool's group is off, because its setting is empty (§6). The startup log names it:

```text
loki tools disabled on mcp: loki_query_logs, loki_metric_query, loki_list_label_values, loki_correlate_changes (LOKI_URL not set)
```

Set the setting, then restart sre-agent. Claude Code caches a server's tool list when a session starts, so a tool or
parameter added by a deploy appears only in a new session.

### 10.8 A call succeeds but returns nothing useful

- **`runbook_search` returns "No relevant runbook content found" right after a re-ingest.** The running process keeps
  its Chroma client and doesn't see the rebuilt store. Restart sre-agent. If it's still empty, run the ingest again and
  check its last line says the store contains documents.
- **`runbook_search` always returns "No relevant runbook content found".** The vector store may never have been
  built. Run `docker compose run --rm sre-ingest` on the sre-agent host, then restart sre-agent.
- **`runbook_search` returns `Runbook search failed (...)`.** The process can't read the store. The message names the
  exception, and `runbooks/rebuilding-the-vector-store.md` covers the permission case.
- **A line reads `unavailable: <reason>`.** That source failed or isn't configured (§7). The reason names which.
- **`upgrade_status` shows no verdicts.** `UPGRADE_ADVISOR_DB_PATH` is empty or the advisor hasn't run yet.

If none of these fit, find the call's `tool_call` line (§9.1). Its `outcome`, `result_chars` and `error` show what the
server saw.
