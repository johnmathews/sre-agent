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
journalctl -u cloudflared -n 50
```

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

### DNS records not resolving

1. Verify CNAME records exist in Cloudflare DNS pointing to the tunnel
2. Check Cloudflare dashboard for DNS propagation
3. Test resolution: `dig <subdomain>.itsa-pizza.com`

## Related Services

- Traefik (reverse proxy receiving traffic from tunnel)
- Immich, Jellyfin (services exposed via tunnel)
