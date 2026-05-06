# Cloudflare Tunnel (cloudflared)

## Purpose

Cloudflare Tunnel exposes selected homelab services to the public internet without opening router ports. The
`cloudflared` daemon runs in a dedicated LXC and creates an encrypted outbound tunnel to Cloudflare's edge network.

## Architecture

```
Internet -> Cloudflare Edge -> Cloudflare Tunnel -> cloudflared LXC (192.168.2.101)
    -> Traefik (reverse proxy) -> Backend services (Immich, Jellyfin, etc.)
```

- cloudflared runs as a native systemd service (not Docker)
- Tunnel is authenticated via a token stored in Ansible vault
- DNS records in Cloudflare point to the tunnel (CNAME to tunnel UUID)
- Some services use Cloudflare Zero Trust access policies; others (Immich, Jellyfin) have bypass policies with Traefik
  rate limiting

## Key Commands

### Check tunnel status

```sh
ssh cloudflared  # root@192.168.2.101
systemctl status cloudflared
sudo journalctl -u cloudflared -n 50
```

### Log retrieval

```sh
# Time-windowed
ssh cloudflared 'sudo journalctl -u cloudflared \
                 --since "2026-05-06 10:08:00" --until "2026-05-06 10:12:00" --no-pager'

# Today's errors and warnings
ssh cloudflared 'sudo journalctl -u cloudflared --since "today" --no-pager | grep -E "ERR|WRN"'

# Errors involving a specific origin
ssh cloudflared 'sudo journalctl -u cloudflared --since "today" --no-pager \
                 | grep "originService=http://192.168.2.108"'

# Errors involving a specific public host
ssh cloudflared 'sudo journalctl -u cloudflared --since "today" --no-pager \
                 | grep "journal-insights.itsa-pizza.com"'
```

`journalctl --since/--until` is host-local time. Timestamps inside log line bodies (e.g. `2026-05-06T05:03:42Z`) are
UTC.

## What cloudflared logs (and what it doesn't)

cloudflared logs **tunnel lifecycle events and connection-level errors only**. There is no access log: successful
requests, response status codes, body sizes, and request paths for healthy traffic are **not recorded**.

Common error patterns:

| Pattern                                                  | Cause                                                                                                |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `stream <N> canceled by remote with error code 0`        | Client closed the HTTP/2 stream before the response finished (browser navigated away, app backgrounded, fetch aborted). Usually benign. |
| `Incoming request ended abruptly: context canceled`      | Same family — client/connection dropped mid-request.                                                |
| `Unable to reach the origin service`                     | Origin (usually traefik) refused or timed out. Tunnel-side problem; pivot to traefik runbook.        |
| `Your version X is outdated`                             | Run an upgrade; not urgent unless tied to other failures.                                            |

**Critical caveat for investigations**: absence of cloudflared entries does **not** prove a request succeeded. It only
rules out a tunnel-level connection failure. A request that received a `502` or `413` from traefik or the origin will
look identical in cloudflared's log to a clean `200` — i.e. invisible. To confirm what the edge actually saw, the
Cloudflare dashboard (Analytics & Logs / HTTP Logs) is the only authoritative source.

Setting `--loglevel debug` would emit per-request lines but is far too noisy for steady-state and is **not** the default.

### Ingress map

`/etc/cloudflared/config.yml` lists every public hostname → internal `service:` URL. Read it before debugging a hostname
to confirm which origin a request would have been forwarded to. The file is **Ansible-managed** — edit the role, not the
file in place (it gets reverted on the next run).

### Restart the tunnel

```sh
ssh cloudflared  # root@192.168.2.101
systemctl restart cloudflared
```

### Deploy via Ansible

```sh
make cloudflared
```

## Prometheus Metrics

cloudflared itself does not expose Prometheus metrics by default. Monitor tunnel health indirectly:

```promql
# LXC host health (192.168.2.101)
up{instance=~".*101.*"}

# CPU/memory on the cloudflared LXC
rate(node_cpu_seconds_total{instance=~".*101.*", mode!="idle"}[5m])
node_memory_MemAvailable_bytes{instance=~".*101.*"}

# Network traffic through the tunnel LXC (spikes = traffic flowing)
rate(node_network_receive_bytes_total{instance=~".*101.*", device!="lo"}[5m])
rate(node_network_transmit_bytes_total{instance=~".*101.*", device!="lo"}[5m])
```

### Agent strategy for "is the tunnel healthy?"

1. Check if the LXC is up via `up{instance=~".*101.*"}`
2. Check network traffic — zero transmit bytes for 5+ minutes suggests tunnel is down or idle
3. Use Loki to check cloudflared service logs: `{hostname=~".*cloudflared.*"} |= "error"` or
   `{hostname=~".*cloudflared.*"} |= "reconnect"`
4. Cross-reference with Traefik — if Traefik shows no incoming requests but services are healthy, the tunnel may be the
   bottleneck

## Troubleshooting

### Services unreachable from internet

1. Check cloudflared service is running: `systemctl status cloudflared`
2. Check service logs: `journalctl -u cloudflared --tail 50`
3. Verify tunnel is connected in Cloudflare dashboard (Zero Trust > Access > Tunnels)
4. Check Traefik is running and routing correctly (see traefik runbook)
5. Test internal connectivity: `curl -v http://<backend-service-ip>:<port>` from the cloudflared LXC

### Tunnel keeps reconnecting

1. Check network connectivity from LXC: `ping 1.1.1.1`
2. Check DNS resolution: `nslookup cloudflare.com`
3. Review service logs for authentication errors: `journalctl -u cloudflared -n 100`
4. Verify tunnel token hasn't expired — regenerate in Cloudflare dashboard if needed

### A request appears to have failed but cloudflared has no log entry

Don't assume the request never reached cloudflared. Cloudflared only records **connection-level** failures:

1. Cross-check whether the **origin** (traefik or direct backend) saw the request — if yes, cloudflared forwarded it
   fine.
2. Check **traefik's** access log for the same time window (see traefik-reverse-proxy runbook). A 4xx/5xx from traefik
   would be invisible to cloudflared.
3. If neither cloudflared nor traefik nor the origin recorded the request, the failure was either upstream of cloudflared
   (Cloudflare edge: see HTTP Logs in the Cloudflare dashboard) or client-side (browser aborted before the request
   left). Cross-reference with the Cloudflare dashboard.

See the `request-failure-investigation` runbook for the full top-to-bottom playbook.

### DNS records not resolving

1. Verify CNAME records exist in Cloudflare DNS pointing to the tunnel
2. Check Cloudflare dashboard for DNS propagation
3. Test resolution: `dig <subdomain>.itsa-pizza.com`

## Related Services

- Traefik (reverse proxy receiving traffic from tunnel)
- Immich, Jellyfin (services exposed via tunnel)
