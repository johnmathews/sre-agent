# Tool Reference

The MCP server exposes up to 36 tools across 14 categories: the 13 groups in the table below (all but Documentation) and the 2 MCP-only conversation history tools. "Up to" means the maximum: each tool group is registered only when its configuration is set (see below), so a deployment shows fewer.

## Server Instructions and Descriptions

Guidance that applies to every tool is sent once, as the MCP `instructions` string in the `initialize` result (`SERVER_INSTRUCTIONS` in `src/api/mcp_server.py`). It states what the server covers, that every tool is read-only except `memory_record_incident`, that clients call `get_current_time` before building a time range, and the accepted time forms (`now`, `now-1h`, `-1h`, `1h`, RFC3339, Unix seconds). Tool descriptions hold only facts about their own tool and never repeat a shared preamble. The SDK agent does not receive these instructions: its system prompt (`src/agent/system_prompt.md`) already tells it to use `get_current_time`, and each time argument documents its accepted forms.

## Conditional Registration

Tools are registered at startup based on which environment variables are set. If a URL is empty or unset, that tool group
is skipped entirely (no failed connections, no error logs).

| Tool Group       | Tools                                                                                                      | Required Config             | Always Available          |
| ---------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------- | ------------------------- |
| Time             | `get_current_time`                                                                                         | None                        | Yes                       |
| Prometheus       | `prometheus_search_metrics`, `prometheus_instant_query`, `prometheus_range_query`, `scrape_targets`, `container_status` | `PROMETHEUS_URL`            | Yes (required)            |
| Grafana Alerting | `grafana_get_alerts`, `grafana_get_alert_rules`                                                            | `GRAFANA_URL`               | Yes (required)            |
| Grafana Dashboards | `grafana_get_dashboard`, `grafana_search_dashboards`                                                     | `GRAFANA_URL`               | Yes (required)            |
| Upgrade Status   | `upgrade_status`                                                                                           | `PROMETHEUS_URL`; `UPGRADE_ADVISOR_DB_PATH` adds verdicts and deploys | Yes |
| Service Health   | `service_health`                                                                                           | `PROMETHEUS_URL`, `GRAFANA_URL`; `LOKI_URL` adds the error count | Yes |
| Loki             | `loki_query_logs`, `loki_metric_query`, `loki_list_label_values`, `loki_correlate_changes`                 | `LOKI_URL`                  | No                        |
| Proxmox VE       | `proxmox_list_guests`, `proxmox_get_guest_config`, `proxmox_node_status`, `proxmox_list_tasks`             | `PROXMOX_URL`               | No                        |
| TrueNAS SCALE    | `truenas_pool_status`, `truenas_list_shares`, `truenas_snapshots`, `truenas_system_status`, `truenas_apps` | `TRUENAS_URL`               | No                        |
| HDD Power Status | `hdd_power_status`                                                                                         | `TRUENAS_URL`               | No                        |
| PBS              | `pbs_datastore_status`, `pbs_list_backups`, `pbs_list_tasks`                                               | `PBS_URL`                   | No                        |
| Memory           | `memory_search_incidents`, `memory_record_incident`, `memory_get_previous_report`, `memory_check_baseline` | `MEMORY_DB_PATH`            | No                        |
| RAG              | `runbook_search`                                                                                           | Chroma vector store on disk | Yes (after `make ingest`) |
| Documentation    | `search_docs`, `query_docs`, `get_document`, `list_sources`                                                | `DOCUMENTATION_MCP_URL`     | No (SDK path only)        |

Health checks (`GET /health`) also follow this pattern — only configured services are checked.

## MCP Server Endpoint

Every tool in the table above except Documentation is also served by a Streamable HTTP MCP server at `/mcp`, under
the same conditions, together with the two conversation history tools, which are on `/mcp` only. This allows MCP
clients to call individual tools directly without the agent loop. Auth is handled by Cloudflare Access.

**How a tool reaches `/mcp`:** each tool is listed once in `TOOL_SPECS` in `src/agent/tool_registry.py`, with the
setting that enables it. `src/api/mcp_server.py` adds one MCP tool per enabled spec:

- Name and description are the LangChain tool's own. The input schema is its Pydantic `args_schema`, so every
  parameter carries its description and its limits (`minimum`, `maximum`, `minLength`, `pattern`, ...).
- No output schema: results are plain text.
- Annotations: `readOnlyHint: true` on every tool except `memory_record_incident` (`readOnlyHint: false`,
  `destructiveHint: false`, `idempotentHint: false`). `openWorldHint: false` on the tools that read only local state
  (`get_current_time`, `runbook_search`, the memory and conversation tools).
- Every call is validated against the schema before the tool runs. Invalid arguments get an `isError` result that
  names each field, e.g. `Invalid arguments for loki_query_logs: limit: Input should be less than or equal to 1000`.
- Argument names the schema does not declare are refused too (each schema says `additionalProperties: false`), and
  the error lists the accepted names, so a guessed name such as `since` never silently falls back to defaults.
  A tool's own failure (`ToolException`) gets an `isError` result with its message.

To add a tool: write it as a LangChain `@tool` with a Pydantic `args_schema` whose fields all have descriptions, add a
`ToolSpec` to `TOOL_SPECS`, add its name to `EXPECTED_NAMES` in `tests/test_tool_registry.py`, and regenerate the
contract snapshot with
`UPDATE_MCP_SNAPSHOT=1 uv run pytest tests/test_mcp_server_integration.py -k test_mcp_contract_matches_snapshot`.

**Access patterns:**
- **`POST /ask`** — question → agent selects tools → synthesized answer (full SRE experience)
- **`/mcp`** — client calls tools directly (lower latency, composable, no agent overhead)

**Claude Code setup:**
```bash
claude mcp add --transport http \
  --header "CF-Access-Client-Id: <id>" \
  --header "CF-Access-Client-Secret: <secret>" \
  -- sre-agent https://sre-mcp.itsa-pizza.com/mcp
```

## Time (always enabled)

### get_current_time

Return the current date, time, weekday, and the user's local time. Used by the agent to ground "how long ago" claims and to verify the day-of-week for past events instead of deriving it by mental modular arithmetic (a known LLM failure mode).

- **Input:** none
- **Returns:** UTC ISO timestamp, UTC epoch seconds, weekday name, today's date, the user's IANA timezone, and the user's local time
- **Example questions where the agent calls this:** "How long has truenas been up?", "When did this incident start, in local time?", "What day was the last backup?"
- **User timezone:** controlled by `USER_TIMEZONE` env var (default `UTC`). Used by both this tool and the system prompt's "Current Date and Time" section.

## Prometheus (always enabled)

### prometheus_search_metrics

Search for available Prometheus metric names matching a substring.

- **Input:** `search_term` (string)
- **Example questions:** "What MikroTik metrics are available?", "Find CPU-related metrics"
- **Returns:** Matching metric names with type and description from metadata

### scrape_targets

List Prometheus scrape targets and their health (`GET /api/v1/targets?state=active`).

- **Input:** `job` (optional; letters, digits and `_ : . -`, at most 100; sent as `scrapePool` and applied again to
  `labels.job`), `only_down` (default `false`; list only targets whose health is `down`)
- **Example questions:** "Which scrape targets are down?", "Is the fastapi job being scraped?"
- **Returns:** A summary line (`N targets, M down across K jobs`), then one line per target with down targets first, then
  unknown, then up: job, instance, hostname, health, last scrape in UTC with its age, scrape duration and `lastError` cut
  to 200 characters. At most 100 targets are listed, with `showing N of M` when more match.
- **Errors:** An invalid `job`, an unreachable Prometheus, a timeout or an HTTP error raises a tool error.

### prometheus_instant_query

Query Prometheus for the current value of a metric (instant query).

- **Input:** `query` (PromQL string), `time` (optional; same forms as the range query's `start`, defaults to now)
- **Example questions:** "What is the current CPU usage on jellyfin?", "How many VMs are running?"
- **Returns:** Formatted metric values with labels. At most 50 series print, with "showing 50 of M series" above
  them. cadvisor's `container_label_*` labels (Compose metadata) are hidden unless the query names one, and one note
  says how many were hidden.
- **Safeguard:** If the query uses `max_over_time` and returns negative values (or wraps `max_over_time` in `abs()`), a
  warning is appended to the output explaining that `min_over_time` + `abs()` should be used instead for peak magnitude
  of negative metrics (e.g. download bytes/sec)
- **Container-status series:** `container_state`, `container_health` and the other container-status exporter metrics
  carry `hostname="infra"` (the exporter's host); the container's host is in `exported_hostname`. The description
  says so and points to `container_status`.
- **Baseline enrichment:** when the memory store is configured, stored baselines for the returned metrics are
  appended. A failure there logs a WARNING and the result returns without them.

### prometheus_range_query

Query Prometheus for metric values over a time range.

- **Input:**
  - `query` (PromQL).
  - `start` (default `1h`) and `end` (default `now`): `now`, a duration before now (`24h`, `-24h`, `now-24h`,
    compound `1h30m`), RFC3339, or Unix seconds. The tool resolves them and sends Unix seconds to Prometheus.
  - `step` (optional): a duration (`15s`, `5m`, `1h30m`) or seconds, at most `1d`. Omitted, the tool picks
    `max(15s, range/250)` rounded up to 15s, 1m, 5m, 15m, 1h, 6h or 1d, so a series has at most 250 points.
- **Limits:** the range may span up to 90 days (Prometheus retention). An explicit step must keep the range under
  11,000 points; the error names the smallest step that fits. Every validation error lists the accepted forms.
- **Example questions:** "How has CPU changed over the last hour?", "Show memory usage for the past day"
- **Returns:** Time series data with sample counts and per-series summary statistics (min, max, avg). Sample times
  print as `YYYY-MM-DD HH:MM:SSZ`. The series cap and `container_label_*` hiding match the instant query.

### container_status

Report Docker container state across hosts from the container-status exporter, with start times from cadvisor.

- **Input:**
  - `host` (optional, at most 64 characters: letters, digits, space and `. _ -`): matched through `host_key`, so
    `media`, `media vm` and `Media VM` name the same host.
  - `name` (optional, at most 128 characters): a case-insensitive substring of the container name.
  - `only_problems` (default `false`): list only containers that are not running, are unhealthy, restarted in the
    last 24 hours or run an outdated image.
- **Data:** nine concurrent instant queries: `container_state`, `container_health`, `container_restart_count`,
  `increase(container_restart_count[86400s])`, `container_image_outdated` and `container_image_info` (all
  `job="container-status"`), cadvisor `container_start_time_seconds`, `up{job="container-status"}` and
  `portainer_exporter_last_scrape_timestamp`. The exporter's series carry `hostname="infra"` (the exporter's own host)
  and the container's host in `exported_hostname`, so the tool keys each container on
  `(host_key(exported_hostname), container_name)` and joins cadvisor's `(host_key(hostname), name)` to it. It filters
  in Python, never on the `hostname` label.
- **Example questions:** "Is jellyfin running?", "Which containers on media have problems?", "When did tubearchivist
  last start?"
- **Returns:** A summary line, the exporter's state (`up` and the last collection time with its age; flagged stale when
  `up` is 0 or missing, or the last collection is over 5 minutes old), then one line per container with problems first:
  `host/name: state[, health], restarts N (+M in 24h), started <UTC> (<age>), image <ref> (<version>)`, prefixed with
  `PROBLEM (reasons)` when it has any; an outdated image reads `(<current> -> <available> available)`. At most 150 containers print, with `showing 150 of M`. A source that fails prints
  `<source>: unavailable: <reason>` and the other sources still render. An unknown `host` lists the known hosts.
- **Errors:** If `container_state` itself fails, the tool raises an error that names every failed source.
- **For other tools:** `collect_containers(host=None, name=None, restart_window_seconds=86400)` in
  `src/agent/tools/containers.py` returns the same data as records (`ContainerReport`), never raises for an HTTP or
  response error, and lists failed sources in `unavailable`. `restart_window_seconds` sets the window of the restart
  delta (`restarts_24h` on each record). `container_status` keeps 24h; `service_health` passes its own window.

## Grafana Alerting (always enabled)

### grafana_get_alerts

Fetch current alerts from Grafana's alerting system.

- **Input:** `state` (optional: "active", "suppressed", "unprocessed", or "firing" as an alias for "active"). Any
  other value is refused with the accepted list, on every call path.
- **Example questions:** "What alerts are firing?", "Are there any active alerts?"
- **Returns:** Alert name, severity, state, labels, annotations, start time. Labels starting with `__` (Grafana
  internals such as `__alert_rule_uid__`) are left out.
- **How it queries:** `GET /api/alertmanager/grafana/api/v2/alerts/groups` with no parameters, then filters by state
  in the tool. The endpoint's `filter` parameter takes label matchers, not states, so `filter=active` returned
  "HTTP 400 bad matcher format".
- **Incident history:** when the memory store is on, past incidents for the listed alert names are appended. A failed
  lookup logs a WARNING and the alerts still print.

### grafana_get_alert_rules

List Grafana alert rules with their current evaluation state and health.

- **Input:** all optional: `state` ("firing", "pending", "inactive", "recovering"), `health` ("ok", "error",
  "nodata"), `name_contains` (case-insensitive substring of the rule name, at most 100 characters). Invalid values are
  refused with the accepted list.
- **Example questions:** "Which alert rules are firing or pending?", "Are any alert rules failing to evaluate?",
  "What alerts are configured?"
- **Returns:**
  - The rule count by state and by health, over all alert rules.
  - "Showing N of M alert rules", where N match the filters.
  - Firing, pending, recovering and unhealthy rules in full: folder, group, severity, UID, last evaluation in UTC
    ("never" before the first one), `lastError` cut to 200 characters, summary, and up to 5 active alert instances
    with their labels (`__`-prefixed labels left out).
  - Inactive rules with health ok on one line each.
  - A field Grafana leaves out prints "not reported by Grafana" instead of failing, and such a rule prints in full.
    States and health are compared in lower case, and the ruler names `Alerting` and `Normal` read as `firing` and
    `inactive`. Recording rules are left out and counted.
- **How it queries:** `GET /api/prometheus/grafana/api/v1/rules` (the Prometheus-compatible rules API), which carries
  state and health. The provisioning API, still used by the weekly report, has neither.

## Grafana Dashboards (always enabled)

### grafana_get_dashboard

Fetch a Grafana dashboard by UID or name, optionally extracting a single panel.

- **Input:** `dashboard` (string — UID or name), `panel` (optional string — panel title, case-insensitive partial match)
- **Example questions:** "What panels does the Home Server dashboard have?", "What query does the CPU panel use?", "Show me the thresholds on the disk usage panel", "Why isn't the hostname filter working on this panel?"
- **Returns:** Full dashboard summary (panels, template vars, datasources, annotations, links) or single panel details (queries, thresholds, units, overrides, transformations)
- **Name lookup:** When `dashboard` contains spaces or doesn't look like a UID, the tool searches by name first via `/api/search`, then fetches the top result. If a UID-style string returns 404, it falls back to search. Every UID, including one
  taken from a search result, is percent-encoded in the request path, so it cannot reach another API path.

### grafana_search_dashboards

Search for Grafana dashboards by name.

- **Input:** `query` (string)
- **Example questions:** "What dashboards exist?", "Find the network dashboard"
- **Returns:** Matching dashboards with title, UID, folder, URL, tags

## Upgrade Status (always enabled)

### upgrade_status

Report what the Upgrade Advisor decided about container image upgrades, and which images are outdated. Read-only: it
never approves or deploys, and its module imports only the advisor's `store` and `models`, never the deploy code.

- **Input:** `container` (optional; letters, digits and `_ . -`, at most 128), `days` (int, default 30, 1–365; how far
  back to list deploys)
- **Example questions:** "Should I upgrade homepage?", "What did the upgrade advisor say about sonarr?", "Which
  containers have a newer image?"
- **Sources, fetched concurrently:**
  - The advisor's SQLite store at `UPGRADE_ADVISOR_DB_PATH`, opened read-only (`mode=ro`, no schema script): the last
    successful run, the latest verdict per container with that verdict's deploys, and other deploys in the last `days`.
  - The container-status exporter in Prometheus: `container_image_outdated` and `container_image_info`, filtered on
    `container_name` and joined on `exported_hostname` (the real host; the exporter's own `hostname` is `infra`).
- **Returns:** The last successful advisor run in UTC with its age. Then each latest verdict (`upgrade`, `hold` or
  `needs_you`), with running and candidate versions, review time and summary, and its deploys (outcome, trigger state,
  detail). Then other deploys in the window, and image freshness (newer image available with current and available
  versions, image, status, base image). With `container`, only that container. Without it, every reviewed container
  (newest verdict first, at most 50) and only the outdated images, with `N of M containers have a newer image`.
- **Errors:**
  - An empty `UPGRADE_ADVISOR_DB_PATH` prints `Upgrade Advisor not configured` and still shows the exporter data.
  - A configured path whose file is missing prints `Upgrade Advisor DB unavailable: file not found: <path>`. An
    unreadable file prints the SQLite error. The exporter data still shows in both cases.
  - A Prometheus failure prints `unavailable: <reason>` in the image freshness part and the advisor data still shows.
  - When neither source answers, the tool raises an error naming both reasons.

## Service Health (always enabled)

### service_health

Answer "is this service up?" in one call: five sources run concurrently and a fixed rule set turns them into one
verdict.

- **Input:**
  - `name` (1–128 characters: letters, digits, space and `. _ ( ) -`): the service, e.g. `jellyfin` or
    `Home Assistant`.
  - `window` (default `1h`, from `5m` to `7d`): how far back the restart count, the Gatus success ratio and the Loki
    error count look. A value outside the range, or not a duration, is refused with the accepted range.
- **Sources and matching (fixed):**
  1. **Gatus:** `gatus_results_endpoint_success` now and `avg_over_time(...[window])`. An endpoint matches when `name`
     equals its `name` label, or its `key` with the `<group>_` prefix removed (`apps_home-assistant` gives
     `home-assistant`), ignoring case.
  2. **Containers:** `collect_containers(name=…, restart_window_seconds=<window>)` from `container_status`: a
     case-insensitive substring of the container name, with restarts counted over the window.
  3. **Blackbox probes:** `probe_success` whose `instance` (the probed URL) contains `name`, ignoring case.
  4. **Grafana alerts:** active alerts whose alert name, a label value (`container_name`, `exported_hostname`, ...) or
     summary contains `name`, ignoring case.
  5. **Loki errors:** `sum by (service_name) (count_over_time({service_name=~"(?i).*<name>.*",
     detected_level=~"error|err|fatal|critical|crit"}[window]))`. The name is regex-escaped, so `a.b(c)` matches only
     itself. Without `LOKI_URL` this source reads `unavailable: LOKI_URL not set`. No error lines reads `no match`.
- **Verdict (fixed rules):**
  - `DOWN`: a matched Gatus endpoint is failing now, or a matched container is not running.
  - `DEGRADED`: a matched container is unhealthy or restarted in the window, a Gatus success ratio is below 1 over
    the window, a matched probe's `probe_success` is 0 now (`probe <instance> failing now`), a matched Grafana alert
    is active, or Loki counts error lines.
  - `UP`: at least one source matched and none of the above holds.
  - `UNKNOWN`: no source matched.
- **Example questions:** "Is jellyfin healthy?", "Has Home Assistant had problems in the last day?"
- **Returns:** The verdict line first, as `<VERDICT>: <name> over <window>: <reasons>`. `DOWN` and `DEGRADED` list
  the reasons that decided them; `UP` names the sources that matched; `UP` and `UNKNOWN` also name any unavailable
  source. Then one line per source: `ok: …`, `no match` or `unavailable: <reason>`.
- **Errors:** An HTTP or response failure in one source makes that source `unavailable` and the verdict still renders.
  The containers source is `unavailable` only when `container_state` fails. Any other failed container query is
  noted in its line (`ok: …; container_image_info unavailable: HTTP 500 - …`) and the containers still decide, so a
  container that is not running still gives `DOWN`. Any other exception propagates.

## Loki (enabled when `LOKI_URL` is set)

### loki_query_logs

Query Loki for log lines using LogQL.

- **Input:** `query` (LogQL string), `start` (default "1h") and `end` (default "now"): `now`, a duration back from now
  (`1h`, `-1h`, `now-1h`, `1h30m`), an ISO 8601 timestamp or Unix epoch seconds; `limit` (int, default 100, max 1000),
  `direction` ("forward" or "backward", default "backward")
- **Example questions:** "Show me recent logs from traefik", "What errors occurred on the media VM in the last 6 hours?"
- **Returns:** Each stream's labels once as a `# hostname=..., service_name=...` header, then `[YYYY-MM-DD HH:MM:SSZ] text`
  lines (each line cut at 500 characters). Output stops at `limit` lines or 60,000 characters and then says
  `showing N of M lines`.
- **Note:** An empty result names the window and says a valid selector can be quiet: widen `start`, or check values with
  `loki_list_label_values`. It does not blame the selector.

### loki_metric_query

Run a LogQL metric query against Loki to count, rate, or aggregate logs. Returns numeric results, not log lines.

- **Input:** `query` (LogQL metric query), `start` (relative or ISO, default "1h"), `end` (relative or ISO, default
  "now"), `step` (optional, e.g. "5m" — if provided, runs range query; if omitted, runs instant query)
- **Example questions:** "Which host has the most logs today?", "What is the error rate per service?", "How many warnings
  in the last hour?"
- **Returns:** For instant queries: series sorted by value descending. For range queries: time series per series with
  timestamps and values.
- **Note:** `count_over_time`, `rate`, `sum by`, `topk` are LogQL functions that run against Loki — never send them to
  Prometheus tools.

### loki_list_label_values

List available values for a Loki label.

- **Input:** `label` (a Loki label name, `^[A-Za-z_][A-Za-z0-9_]*$`, e.g. "hostname", "service_name"; anything else is rejected before a request is sent), `query` (optional LogQL stream selector to scope results),
  `start` (default `"6h"`, Loki's own default), `end` (default `"now"`); both take the same forms as `loki_query_logs`
  and reach Loki as nanosecond `start`/`end`
- **Example questions:** "What services are running on the infra VM?", "What hostnames send logs?"
- **Returns:** Sorted list of values for the given label, headed with the range, e.g. `Found 31 values for label
  'container' in the last 6h:`. At most 200 values print, with `showing 200 of M`.
- **Note:** Only values with log lines in the range are listed. A quiet container is absent from the default 6h, so
  widen `start` (e.g. `7d`) before concluding something ships no logs.

### loki_correlate_changes

Search for significant log events around a reference time for change correlation.

- **Input:** `reference_time` (ISO timestamp or "now"), `window_minutes` (int, default 30), `hostname` (optional filter),
  `service_name` (optional filter)
- **Example questions:** "What changed before this alert fired?", "Show me what happened around 2pm on the infra VM"
- **Returns:** A header (UTC window, filters, counts, warnings), then a chronological timeline (`[YYYY-MM-DD HH:MM:SSZ]`,
  oldest first) and a summary by service. Three queries run concurrently:
  - **Error and warning lines:** `detected_level=~"error|err|fatal|critical|crit|warn|warning"` (the shared
    `ERROR_LEVELS` and `WARN_LEVELS`), newest 200 first (`direction=backward`). The `docker-events` stream is left out.
  - **Line count:** an instant query `sum(count_over_time(<error selector>[<window>s]))` at the reference time gives
    the total M. When the 200-line cap cuts the result the header says `showing newest N of M error/warn lines`. If
    the count fails the header leaves out M and gives the reason. A failed count does not mark the timeline
    incomplete.
  - **Container lifecycle events:** the newest 100 matching lines of the `{service_name="docker-events"}` stream
    (`docker events` JSON). Only `Type=container` with action `start`, `stop`, `restart`, `die`, `kill`, `oom` or
    `health_status: unhealthy` counts, shown as e.g. `die exitCode=137` or `kill signal=15`. Ordinary log lines that
    contain "started" are not events. With `service_name`, an event counts when the container name or its Compose
    service equals it. A returned line that is not docker events JSON is counted in the header.
- **Failures:** if one query fails the header says `WARNING: <which> query failed: <reason>; the timeline is
  incomplete`. If both fail the tool returns an error.
- **Quoting:** `hostname` and `service_name` are LogQL-quoted (`promql_string`), so a quote or backslash cannot change
  the query.

## Proxmox VE (enabled when `PROXMOX_URL` is set)

### proxmox_list_guests

List all VMs and containers on the Proxmox node.

- **Input:** `guest_type` (optional, enum `"qemu"` or `"lxc"`; anything else is rejected)
- **Example questions:** "What VMs are running?", "List all containers", "How many guests are there?"
- **Returns:** VMID, name, type (VM/CT), status, vCPUs, RAM, CPU usage. For stopped guests, shows config
  allocations with a note that no host resources are consumed (CPU% is omitted since it's zero)

### proxmox_get_guest_config

Get the full configuration of a specific VM or container.

- **Input:** `name` (string, preferred — auto-resolves VMID and type) OR `vmid` (integer) + `guest_type` (enum `"qemu"` or `"lxc"`, default
  `"qemu"`)
- **Example questions:** "What disks does VM 100 have?", "Show the config for jellyfin"
- **Returns:** Grouped config: compute (CPU/RAM), disks, network, boot/OS settings
- **Name lookup:** When `name` is provided (e.g. "immich"), the tool lists all guests to resolve the correct VMID and
  type (qemu/lxc) automatically. This avoids needing to call `proxmox_list_guests` first.

### proxmox_node_status

Get overall status of the Proxmox host node.

- **Input:** none
- **Example questions:** "How is the Proxmox server doing?", "What PVE version is running?"
- **Returns:** CPU (node_exporter 5-minute average from Prometheus, or `unavailable: <reason>`), CPU (PVE instant
  sample), memory, root FS, load average, uptime, PVE version, kernel

### proxmox_list_tasks

List recent Proxmox tasks (migrations, backups, snapshots, etc).

- **Input:** `limit` (int, default 20), `errors_only` (bool, default false)
- **Example questions:** "Any recent failed tasks?", "Is a migration running?"
- **Returns:** Task type, status, user, start/end time, guest ID

## TrueNAS SCALE (enabled when `TRUENAS_URL` is set)

### truenas_pool_status

Get ZFS pool health and dataset space usage from TrueNAS.

- **Input:** none
- **Example questions:** "Is the tank pool healthy?", "How much space is left on the NAS?", "Any degraded pools?", "How
  big is my photos dataset?", "How much space does tank/media use?"
- **Returns:** Pool status (ONLINE/DEGRADED/FAULTED), health flag, size/used/free space, disk topology, per-dataset
  used/available for each top-level dataset

### truenas_list_shares

List NFS and SMB shares configured on TrueNAS.

- **Input:** `share_type` (optional: "nfs" or "smb")
- **Example questions:** "What NFS shares exist?", "Is the document-store share enabled?", "Which SMB shares are configured?"
- **Returns:** Share path, enabled/disabled status, read-only flag, allowed networks/hosts, comments

### truenas_snapshots

Report ZFS snapshot counts, periodic snapshot tasks with their last run, and replication tasks on TrueNAS.

- **Input:** `dataset` (optional, e.g. "tank/media"), `list_snapshots` (bool, default false; needs `dataset`), `limit`
  (int 1–200, default 50; how many of the newest snapshots `list_snapshots` shows)
- **Example questions:** "When did the tank/media snapshot task last run?", "How many snapshots does tank have?", "Is
  replication running?", "What snapshot schedules exist?"
- **Returns:**
  - The snapshot count per pool.
  - Each periodic snapshot task: last run (state and UTC time with age, or "not reported by TrueNAS"), schedule,
    retention, recursive flag, and the count for its dataset. That count covers the dataset itself, not its children.
  - Each replication task: its state and the time of that state.
  - With `dataset`: that dataset's own count and its pool's.
  - With `list_snapshots=true`: the dataset's snapshots, newest first by creation time, with "showing newest N of M".
  - A count or listing that fails prints "unavailable: <reason>" and the rest still prints.
- **Speed:** Counts and last-run times are fast. The listing takes about 20 s (45 s timeout), so it runs only when asked.
- **How it queries:** `GET /zfs/snapshot?count=true` with `pool=<p>` or `dataset=<d>`, which returns a bare integer.
  The listing is `GET /zfs/snapshot?dataset=<d>` with no limit, and it is sorted in the tool. The tool never sends
  `query-filters` or `query-options`. Those are not REST v2.0 parameters: TrueNAS treats them as a filter that matches
  nothing, which used to produce a false "0 snapshots".
- **Requires https:** `TRUENAS_URL` must be an `https://` URL. With any other scheme, every TrueNAS tool, and
  `hdd_power_status`, refuses to send the API key and makes no request: "TrueNAS URL must be https; refusing to send
  the API key over plain http".

### truenas_system_status

Get TrueNAS system information, alerts, running jobs, and disk inventory.

- **Input:** none
- **Example questions:** "Any TrueNAS alerts?", "What version is TrueNAS running?", "What disks does TrueNAS have?"
- **Returns:** Version, hostname, uptime, hardware, active alerts, running jobs with progress, disk inventory

### truenas_apps

List apps installed on TrueNAS SCALE with their running state.

- **Input:** none
- **Example questions:** "What apps are running on TrueNAS?", "Is Alloy running?", "Is the disk-status-exporter
  deployed?"
- **Returns:** App name, state (RUNNING/STOPPED/DEPLOYING), version, upgrade availability

## HDD Power Status (enabled when `TRUENAS_URL` is set)

### hdd_power_status

Complete HDD power status summary: current state, human-readable disk names, and transition history.

- **Input:**
  - `duration` (string, default `"24h"`) — time window for stats and transition history. Examples: `"1h"`, `"6h"`,
    `"12h"`, `"24h"`, `"3d"`, `"1w"`.
  - `pool` (string or null, default `null`) — optional ZFS pool name filter (e.g. `"tank"`, `"backup"`). If omitted, all
    HDD pools are included.
- **Example questions:** "Which HDDs are spun up?", "Are the backup pool drives spun down?" (`pool='backup'`), "How many
  state changes in the last 12 hours?" (`duration='12h'`), "Were the tank HDDs active this week?"
  (`duration='1w', pool='tank'`), "What fraction of the last 6h were my drives in standby?" (`duration='6h'`)
- **Returns:** Per-disk power state (standby, idle, active_or_idle, idle_a/b/c, active, sleep, error, unknown) with
  model, size, serial, pool. Change counts and time-in-state percentages for the requested duration. All power state
  transitions in chronological order per disk, with exact timestamps and from/to state labels. Automatically
  cross-references Prometheus `disk_power_state` with TrueNAS disk inventory, enriches pool assignments from `/pool`
  topology (since `/disk` returns pool as null). Fetches range data once and computes both stats and transitions from
  the same data. Uses progressive widening (skipping already-covered windows) to find transitions beyond the requested
  duration. Prometheus and TrueNAS API calls are parallelized using `asyncio.create_task`.

## Proxmox Backup Server (enabled when `PBS_URL` is set)

### pbs_datastore_status

Get storage usage for all PBS datastores.

- **Input:** none
- **Example questions:** "How much backup space is left?", "Is the backup store full?"
- **Returns:** Datastore name, total/used/available space, usage%, last GC status

### pbs_list_backups

List backup groups in a PBS datastore.

- **Input:** `datastore` (optional, defaults to `PBS_DEFAULT_DATASTORE`; letters, digits, `.`, `_`, `-`, up to 64
  characters, not starting with `.` or `-`; anything else is refused before any request)
- **Example questions:** "When was VM 100 last backed up?", "Which VMs are being backed up?"
- **Returns:** Backup type (VM/CT/Host), ID, snapshot count, last backup as a UTC date with its age (`2026-11-10 22:13:20Z (1d ago)`), owner

### pbs_list_tasks

List recent PBS tasks (backup jobs, GC, verification).

- **Input:** `limit` (int, default 20), `errors_only` (bool, default false)
- **Example questions:** "Did last night's backup succeed?", "Any failed backup tasks?"
- **Returns:** Task type, status, user, start/end time (UTC, `YYYY-MM-DD HH:MM:SSZ`), worker ID

## Agent Memory (enabled when `MEMORY_DB_PATH` is set)

### memory_search_incidents

Search past incidents recorded in the agent's memory store.

- **Input:** `query` (optional keyword), `alert_name` (optional exact match), `service` (optional substring), `limit`
  (int, default 10)
- **Example questions:** "Has this HighCPU alert happened before?", "What incidents involved traefik?"
- **Returns:** Matching incidents with title, description, root cause, resolution, severity, and status

### memory_record_incident

Record a new incident in the agent's memory store.

- **Input:** `title` (string), `description` (string), `alert_name` (optional), `root_cause` (optional), `resolution`
  (optional), `severity` (info/warning/critical), `services` (comma-separated)
- **Example use:** After identifying a root cause during investigation, record it for future reference
- **Returns:** Confirmation with incident ID

### memory_get_previous_report

Retrieve the most recent archived weekly reliability report(s).

- **Input:** `count` (int, default 1, max 10)
- **Example questions:** "What did last week's report say?", "Show me the last 3 reports"
- **Returns:** Full report markdown (count=1) or summary with key metrics (count>1)

### memory_check_baseline

Check whether a metric value is within the normal range based on computed baselines.

- **Input:** `metric_name` (string), `current_value` (float), `labels` (optional JSON string)
- **Example questions:** "Is 85% CPU normal for the media VM?"
- **Returns:** Baseline statistics (avg, p95, min, max) and assessment (WITHIN NORMAL RANGE / ABOVE P95 / BELOW MIN)

## RAG (enabled when vector store exists)

### runbook_search

Search operational runbooks for procedures, troubleshooting steps, and architecture docs.

- **Input:** `query` (string)
- **Example questions:** "How do I restart the DNS stack?", "What's the procedure for NFS issues?"
- **Returns:** Relevant runbook chunks with source attribution

## Conversation History (MCP server only)

These tools are registered on the MCP server but not in the agent loop. They allow MCP clients
(e.g., Claude Code) to browse past conversations the deployed agent has had via the web UI or API.

### sre_agent_list_conversations

List recent conversations, most-recently-updated first.

- **Input:** `limit` (int, default 20)
- **Returns:** Session ID, title, timestamps, turn count, model, provider

### sre_agent_get_conversation

Retrieve the full dialogue for a specific conversation.

- **Input:** `session_id` (string, e.g. "abc12345")
- **Returns:** Full conversation JSON with all user/assistant turns and timestamps
- **Note:** Use `sre_agent_list_conversations` first to find the session ID
