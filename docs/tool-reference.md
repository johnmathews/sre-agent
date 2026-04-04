# Tool Reference

The agent has up to 28 tools across 10 categories. Tools are conditionally registered based on configuration.

## Conditional Registration

Tools are registered at startup based on which environment variables are set. If a URL is empty or unset, that tool group
is skipped entirely (no failed connections, no error logs).

| Tool Group       | Tools                                                                                                      | Required Config             | Always Available          |
| ---------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------- | ------------------------- |
| Prometheus       | `prometheus_search_metrics`, `prometheus_instant_query`, `prometheus_range_query`                          | `PROMETHEUS_URL`            | Yes (required)            |
| Grafana Alerting | `grafana_get_alerts`, `grafana_get_alert_rules`                                                            | `GRAFANA_URL`               | Yes (required)            |
| Grafana Dashboards | `grafana_get_dashboard`, `grafana_search_dashboards`                                                     | `GRAFANA_URL`               | Yes (required)            |
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

When `MCP_AUTH_TOKEN` is set, all tools (except Documentation) are also exposed as a Streamable HTTP MCP server at
`/mcp`. This allows MCP clients to call individual tools directly without the agent loop.

**Access patterns:**
- **`POST /ask`** — question → agent selects tools → synthesized answer (full SRE experience)
- **`/mcp`** — client calls tools directly (lower latency, composable, no agent overhead)

**Claude Code setup:**
```bash
claude mcp add --transport http sre-assistant \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>" \
  http://<host>:8000/mcp
```

## Prometheus (always enabled)

### prometheus_search_metrics

Search for available Prometheus metric names matching a substring.

- **Input:** `search_term` (string)
- **Example questions:** "What MikroTik metrics are available?", "Find CPU-related metrics"
- **Returns:** Matching metric names with type and description from metadata

### prometheus_instant_query

Query Prometheus for the current value of a metric (instant query).

- **Input:** `query` (PromQL string), `time` (optional RFC3339/Unix timestamp)
- **Example questions:** "What is the current CPU usage on jellyfin?", "How many VMs are running?"
- **Returns:** Formatted metric values with labels
- **Safeguard:** If the query uses `max_over_time` and returns negative values (or wraps `max_over_time` in `abs()`), a
  warning is appended to the output explaining that `min_over_time` + `abs()` should be used instead for peak magnitude
  of negative metrics (e.g. download bytes/sec)

### prometheus_range_query

Query Prometheus for metric values over a time range.

- **Input:** `query` (PromQL), `start`, `end` (timestamps), `step` (duration)
- **Example questions:** "How has CPU changed over the last hour?", "Show memory usage for the past day"
- **Returns:** Time series data with sample counts and per-series summary statistics (min, max, avg)

## Grafana Alerting (always enabled)

### grafana_get_alerts

Fetch active alerts from Grafana's alerting system.

- **Input:** `state` (optional: "active", "suppressed", "unprocessed")
- **Example questions:** "What alerts are firing?", "Are there any active alerts?"
- **Returns:** Alert name, severity, state, labels, annotations, start time

### grafana_get_alert_rules

Fetch alert rule definitions from Grafana.

- **Input:** none
- **Example questions:** "What alerts are configured?", "What conditions trigger the high CPU alert?"
- **Returns:** Rule name, UID, folder, group, severity, summary

## Grafana Dashboards (always enabled)

### grafana_get_dashboard

Fetch a Grafana dashboard by UID or name, optionally extracting a single panel.

- **Input:** `dashboard` (string — UID or name), `panel` (optional string — panel title, case-insensitive partial match)
- **Example questions:** "What panels does the Home Server dashboard have?", "What query does the CPU panel use?", "Show me the thresholds on the disk usage panel", "Why isn't the hostname filter working on this panel?"
- **Returns:** Full dashboard summary (panels, template vars, datasources, annotations, links) or single panel details (queries, thresholds, units, overrides, transformations)
- **Name lookup:** When `dashboard` contains spaces or doesn't look like a UID, the tool searches by name first via `/api/search`, then fetches the top result. If a UID-style string returns 404, it falls back to search.

### grafana_search_dashboards

Search for Grafana dashboards by name.

- **Input:** `query` (string)
- **Example questions:** "What dashboards exist?", "Find the network dashboard"
- **Returns:** Matching dashboards with title, UID, folder, URL, tags

## Loki (enabled when `LOKI_URL` is set)

### loki_query_logs

Query Loki for log lines using LogQL.

- **Input:** `query` (LogQL string), `start` (relative like "1h" or ISO timestamp, default "1h"), `end` (relative or ISO,
  default "now"), `limit` (int, default 100, max 1000), `direction` ("forward" or "backward", default "backward")
- **Example questions:** "Show me recent logs from traefik", "What errors occurred on the media VM in the last 6 hours?"
- **Returns:** Formatted log lines with timestamps, stream labels, and log text

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

- **Input:** `label` (string, e.g. "hostname", "service_name"), `query` (optional LogQL stream selector to scope results)
- **Example questions:** "What services are running on the infra VM?", "What hostnames send logs?"
- **Returns:** Sorted list of values for the given label

### loki_correlate_changes

Search for significant log events around a reference time for change correlation.

- **Input:** `reference_time` (ISO timestamp or "now"), `window_minutes` (int, default 30), `hostname` (optional filter),
  `service_name` (optional filter)
- **Example questions:** "What changed before this alert fired?", "Show me what happened around 2pm on the infra VM"
- **Returns:** Chronological timeline of error/warn/fatal events and container lifecycle events, grouped by service

## Proxmox VE (enabled when `PROXMOX_URL` is set)

### proxmox_list_guests

List all VMs and containers on the Proxmox node.

- **Input:** `guest_type` (optional: "qemu" or "lxc")
- **Example questions:** "What VMs are running?", "List all containers", "How many guests are there?"
- **Returns:** VMID, name, type (VM/CT), status, vCPUs, RAM, CPU usage

### proxmox_get_guest_config

Get the full configuration of a specific VM or container.

- **Input:** `name` (string, preferred — auto-resolves VMID and type) OR `vmid` (integer) + `guest_type` (string, default
  "qemu")
- **Example questions:** "What disks does VM 100 have?", "Show the config for jellyfin"
- **Returns:** Grouped config: compute (CPU/RAM), disks, network, boot/OS settings
- **Name lookup:** When `name` is provided (e.g. "immich"), the tool lists all guests to resolve the correct VMID and
  type (qemu/lxc) automatically. This avoids needing to call `proxmox_list_guests` first.

### proxmox_node_status

Get overall status of the Proxmox host node.

- **Input:** none
- **Example questions:** "How is the Proxmox server doing?", "What PVE version is running?"
- **Returns:** CPU%, memory, root FS, load average, uptime, PVE version, kernel

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
- **Example questions:** "What NFS shares exist?", "Is the paperless share enabled?", "Which SMB shares are configured?"
- **Returns:** Share path, enabled/disabled status, read-only flag, allowed networks/hosts, comments

### truenas_snapshots

List ZFS snapshots, snapshot schedules, and replication tasks on TrueNAS.

- **Input:** `dataset` (optional filter, e.g. "tank/media"), `limit` (int, default 50)
- **Example questions:** "When was the last snapshot of tank/media?", "Is replication running?", "What snapshot schedules
  exist?"
- **Returns:** Recent snapshots, periodic snapshot task schedules with retention, replication task status

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
  model, size, serial, pool. Change counts and time-in-state percentages for the requested duration. Last power state
  change timestamp with from/to transition. Automatically cross-references Prometheus `disk_power_state` with TrueNAS
  disk inventory, enriches pool assignments from `/pool` topology (since `/disk` returns pool as null), and uses
  progressive `changes()` widening to find transitions. API calls to Prometheus and TrueNAS are made concurrently
  using `asyncio.create_task` to minimize total query time.

## Proxmox Backup Server (enabled when `PBS_URL` is set)

### pbs_datastore_status

Get storage usage for all PBS datastores.

- **Input:** none
- **Example questions:** "How much backup space is left?", "Is the backup store full?"
- **Returns:** Datastore name, total/used/available space, usage%, last GC status

### pbs_list_backups

List backup groups in a PBS datastore.

- **Input:** `datastore` (optional, defaults to `PBS_DEFAULT_DATASTORE`)
- **Example questions:** "When was VM 100 last backed up?", "Which VMs are being backed up?"
- **Returns:** Backup type (VM/CT/Host), ID, snapshot count, last backup time, owner

### pbs_list_tasks

List recent PBS tasks (backup jobs, GC, verification).

- **Input:** `limit` (int, default 20), `errors_only` (bool, default false)
- **Example questions:** "Did last night's backup succeed?", "Any failed backup tasks?"
- **Returns:** Task type, status, user, start/end time, worker ID

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
