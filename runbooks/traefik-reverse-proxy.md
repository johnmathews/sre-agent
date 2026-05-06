# Traefik Reverse Proxy

## Purpose

Traefik handles ingress for public-facing services (Immich, Jellyfin) that are exposed through the Cloudflare Tunnel. It
provides rate limiting on authentication and API routes since these services bypass Cloudflare Zero Trust access
policies.

## Architecture

```
Internet -> Cloudflare Edge -> Cloudflare Tunnel -> cloudflared LXC (192.168.2.101)
    -> Traefik (192.168.2.108, Docker) -> Backend services
```

- Only port 443 is exposed to the public internet (via Cloudflare proxy)
- Traefik applies rate limiting to auth/API routes for Immich and Jellyfin
- Traefik runs as a Docker container on a dedicated LXC (192.168.2.108)
- Configuration is file-based (dynamic config in `/srv/apps/traefik/`)
- Dashboard: https://traefik.itsa-pizza.com/dashboard/
- API overview: https://traefik.itsa-pizza.com/api/overview

### Services routed through Traefik

| Service  | Backend Address           | Rate Limited |
| -------- | ------------------------- | ------------ |
| Immich   | http://192.168.2.113      | Auth routes  |
| Jellyfin | http://192.168.2.105:8096 | Auth routes  |

## Key Commands

### Check Traefik status

```sh
ssh traefik  # root@192.168.2.108
docker ps | grep traefik
sudo journalctl CONTAINER_NAME=traefik --no-pager | tail -50
```

**Do not use `docker logs traefik` for anything older than the last few minutes.** The container uses Docker's `journald`
log driver, and `docker logs` only returns whatever Docker still has buffered — typically a small fraction of the real
stream. Always use `journalctl` (or Loki) for historical traefik logs.

### Log retrieval

```sh
# Time-windowed (host-local timezone)
ssh traefik 'sudo journalctl CONTAINER_NAME=traefik \
             --since "2026-05-06 10:08:00" --until "2026-05-06 10:12:00" --no-pager'

# By full container ID — robust if the container was recreated mid-window
ssh traefik 'CID=$(docker inspect traefik --format "{{.Id}}"); \
             sudo journalctl CONTAINER_ID_FULL=$CID --since "1 hour ago" --no-pager'

# Internal/error lines only (drop access-log lines)
ssh traefik 'sudo journalctl CONTAINER_NAME=traefik --since "today" --no-pager \
             | grep -vE "(GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH) /"'
```

Or via Loki — same data, faster, no SSH:

```logql
# All requests to one public host
{hostname="traefik", service_name="traefik"} |= "journal-insights"

# Non-2xx responses
{hostname="traefik", service_name="traefik"} |~ "\" [45][0-9]{2} "
```

Access-log line format:
`<client-ip> - - [<ts>] "<METHOD> <path> HTTP/<v>" <status> <bytes> "<referer>" "<UA>" <id> "<router>" "<origin-url>" <duration>`

Useful fields: `<router>` identifies which traefik router matched (e.g. `journal-insights@docker`), `<origin-url>` is the
upstream traefik proxied to, `<duration>` is `<n>ms` or `<n>s`.

**Timezones**: `journalctl --since/--until` accepts host-local time (CEST on this LXC). The timestamp inside each
access-log line is UTC. Be explicit about which one you're correlating with.

### View active routers and services

```sh
# From any host on the network
curl -s https://traefik.itsa-pizza.com/api/http/routers | jq '.[].name'
curl -s https://traefik.itsa-pizza.com/api/http/services | jq '.[].name'
```

### View dashboard

- https://traefik.itsa-pizza.com/dashboard/

## Prometheus Metrics

Traefik exposes built-in Prometheus metrics when configured.

```promql
# Request rate by service
rate(traefik_service_requests_total[5m])

# Request duration (p95) by service
histogram_quantile(0.95, rate(traefik_service_request_duration_seconds_bucket[5m]))

# Error rate (4xx + 5xx) by service
rate(traefik_service_requests_total{code=~"4..|5.."}[5m])

# Open connections
traefik_service_open_connections

# Host-level health (Traefik LXC)
up{instance=~".*108.*"}
rate(node_cpu_seconds_total{instance=~".*108.*", mode!="idle"}[5m])
```

### Agent strategy for "why is a service slow?"

1. Check Traefik container is running: Loki logs `{hostname=~".*traefik.*"} |= "error"`
2. Check request latency: `histogram_quantile(0.95, rate(traefik_service_request_duration_seconds_bucket[5m]))`
3. Check error rate: `rate(traefik_service_requests_total{code=~"5.."}[5m])` — high 5xx = backend issue
4. Check the backend service directly (Immich, Jellyfin) to isolate whether latency is Traefik or backend
5. Check the cloudflared tunnel — if Traefik metrics look normal, the bottleneck may be upstream

## Troubleshooting

### Service unreachable through Cloudflare

1. Verify cloudflared tunnel is up (see cloudflared-tunnel runbook)
2. Check Traefik container is running: `docker ps | grep traefik`
3. Check Traefik logs for routing errors: `docker logs traefik --tail 100`
4. Verify Traefik config has correct backend service addresses
5. Test backend service directly from the Traefik LXC: `curl http://<backend-ip>:<port>`
6. Check Traefik dashboard for the service's router status

### Rate limiting too aggressive

1. Check Traefik middleware configuration for rate limit settings
2. Review Traefik access logs for blocked requests (HTTP 429 responses)
3. Adjust rate limit values in the Traefik dynamic config file
4. Check if a legitimate client is hitting limits (correlate with Loki access logs)

### TLS certificate issues

1. Traefik handles TLS termination for the Cloudflare tunnel
2. Check certificate status in Traefik dashboard
3. If certs expired, check ACME/Let's Encrypt resolver logs: `docker logs traefik | grep -i acme`

### Traefik silently stops logging while still serving traffic

**Symptom**: container is `Up`, requests are being routed (downstream apps log them, the dashboard works), but
`journalctl CONTAINER_NAME=traefik` has no entries past some point. Loki shows the same gap. Confirmed seen on
2026-05-06: traefik stopped emitting access logs at 09:30 CEST while continuing to serve requests.

**Diagnostic checklist**:

1. Confirm the container is alive and forwarding traffic — `docker ps`, hit a known route in a browser, check
   `traefik_service_requests_total` in Prometheus is still incrementing.
2. Compare last log line vs. container start time:
   ```sh
   ssh traefik 'docker inspect traefik --format "Started: {{.State.StartedAt}}"; \
                sudo journalctl CONTAINER_NAME=traefik --no-pager | tail -1'
   ```
3. Rule out journald rate-limit or a full disk:
   ```sh
   ssh traefik 'sudo journalctl --disk-usage; \
                sudo journalctl --since "1 hour ago" --no-pager 2>&1 \
                | grep -iE "Suppressed|rate-limit|kept"'
   ```
4. Check for a journald restart that could have wedged a long-lived writer:
   ```sh
   ssh traefik 'sudo journalctl -u systemd-journald --since "yesterday" --no-pager | tail'
   ```

**Fix**: `ssh traefik 'docker restart traefik'` reopens the stdout pipe to the journald driver. **Caveat**: this drops
in-flight long-lived streams (the config sets `writeTimeout: 0` for audio/video). Schedule rather than snipe.

**Permanent mitigation** (not yet applied): switch traefik's `accessLog` block to a file path with a bind-mounted
directory, decoupling access logs from the journald driver:

```yaml
accessLog:
  filePath: /var/log/traefik/access.log
  bufferingSize: 100
```

Plus a `/var/log/traefik` bind mount and logrotate.

### Routing misconfiguration

1. Check active routers via API: `curl -s https://traefik.itsa-pizza.com/api/http/routers | jq`
2. Look for routers with `status: disabled` or priority conflicts
3. Verify host rules match the expected domain names
4. Check middleware chain order (rate limiting should come after auth headers)

## Related Services

- Cloudflare Tunnel (upstream traffic source — see cloudflared-tunnel runbook)
- Immich, Jellyfin (backend services)
- Cloudflare Zero Trust (access policies for other services)
- Loki (access logs from Traefik container)
