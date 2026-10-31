# Loki Logging Pipeline

## Purpose

The homelab uses **Grafana Alloy** to collect logs from Docker containers and select systemd journal units, shipping them
to a central **Loki** instance for storage and querying.

## Collection Architecture

```
Docker containers (each VM/LXC)
    └── Alloy (runs on each host)
            └── Loki (central instance)
                    └── SRE Agent (queries via HTTP API)
```

Every VM and LXC that runs Docker has an identical Alloy configuration. Alloy discovers containers automatically via the
Docker socket and attaches standardized labels to every log stream.

## Label Taxonomy

Every log stream has exactly 4 labels:

| Label            | Description                            | Examples                                                |
| ---------------- | -------------------------------------- | ------------------------------------------------------- |
| `hostname`       | VM/LXC name (matches Prometheus label) | media, infra, jellyfin, proxmox                         |
| `service_name`   | Docker Compose service or systemd unit | traefik, adguard, immich-server                         |
| `container`      | Docker container name                  | traefik-traefik-1, adguard-adguardhome-1                |
| `detected_level` | Normalized log level                   | debug, info, notice, warn, error, fatal, verbose, trace |

**Note:** `detected_level` is normalized by Alloy's log level detection. The original application may use different level
names (e.g. WARNING vs warn) — Alloy maps them to the canonical set above.

## What's Collected

- **Docker containers** — all stdout/stderr from every container on every managed VM/LXC
- **Select systemd units** — specific journal units configured per host (e.g. cloudflared on the infra LXC)

## What's NOT Collected

- Kernel logs (dmesg)
- Proxmox VE system logs (pveproxy, pvedaemon)
- TrueNAS system logs
- Auth logs (sshd, PAM)
- Application logs written only to files (not stdout)

## LogQL Quick Reference

### Stream Selectors

```logql
# All logs from a host
{hostname="media"}

# Specific service
{service_name="traefik"}

# Filter by log level
{detected_level="error"}

# Regex match on level
{detected_level=~"error|warn|fatal"}

# Combine filters
{hostname="infra", service_name="traefik", detected_level="error"}
```

### Line Filters

```logql
# Contains substring
{hostname="media"} |= "connection refused"

# Does NOT contain
{hostname="media"} != "healthcheck"

# Regex match
{service_name="traefik"} |~ "(?i)upstream.*(timeout|refused)"

# Negative regex
{service_name="jellyfin"} !~ "(?i)debug|trace"
```

### Common Queries

```logql
# Recent errors from a specific host
{hostname="infra", detected_level=~"error|fatal"}

# Traefik 502 errors
{service_name="traefik"} |= "502"

# Container lifecycle events
{hostname="media"} |~ "(?i)(started|stopped|exited|restarting|killed|oom)"

# All warnings and errors across the homelab
{detected_level=~"warn|error|fatal"}
```

### Metric Queries (Aggregations)

LogQL supports metric queries that return numbers instead of log lines. These use functions like `count_over_time`,
`rate`, `sum by`, and `topk`. They are **Loki queries, not PromQL** — run them via `loki_metric_query`, never via
`prometheus_instant_query`.

```logql
# Top 5 hosts by log volume in the last 24 hours
topk(5, sum by (hostname) (count_over_time({hostname=~".+"}[24h])))

# Error count per service in the last hour
sum by (service_name) (count_over_time({detected_level="error"}[1h]))

# Log rate for a specific host (logs per second)
sum(rate({hostname="media"}[5m]))

# Log volume breakdown by level for a host
sum by (detected_level) (count_over_time({hostname="infra"}[24h]))

# Warning + error rate over time (use with step for time series)
sum by (hostname) (rate({detected_level=~"warn|error"}[5m]))
```

**Instant vs range:** If you need a single answer (e.g., "which host has the most logs?"), omit the `step` parameter for
an instant query. If you need a time series (e.g., "how has error rate changed over the day?"), provide a `step` like
`5m` or `1h`.

## Retention

Loki retention is configured at the server level. Check the Loki configuration for current retention period settings.

## Troubleshooting

### No logs from a host

1. Check if Alloy is running on the host: `systemctl status alloy`
2. Check Alloy logs for errors: `journalctl -u alloy -f`
3. Verify Docker socket access: Alloy needs read access to `/var/run/docker.sock`
4. Use `loki_list_label_values` with label "hostname" to see which hosts are reporting

### Missing service logs

1. Check if the container is running: `docker ps` on the host
2. Verify the container writes to stdout/stderr (not just files)
3. Use `loki_list_label_values` with label "service_name", query `{hostname="<host>"}` and start `7d` to see what
   services are logging on that host (the default range is the last 6h, which misses quiet services)

### Log level detection issues

If `detected_level` shows unexpected values, the application may use a non-standard log format that Alloy can't parse.
The logs are still collected — just the level label may be inaccurate. Filter by service_name instead of detected_level
in these cases.
