# Media VM

## Purpose

Hosts the media indexing/download stack (qBittorrent, Sonarr, Radarr, Prowlarr, Bazarr, sabnzbd, jellyseerr,
flaresolverr), the slskd/booklore/journal-insights stacks, and observability sidecars (alloy, node_exporter, cadvisor,
portainer agent). Outbound traffic is routed through a Gluetun container running the Mullvad provider. Media files are
stored on TrueNAS and mounted via NFS shares.

**Jellyfin does not run here.** It runs on a separate LXC, `jellyfin_lxc` at `192.168.2.110`.

## Architecture

- Media files live on TrueNAS NFS shares, not on the VM itself
- NFS mounts configured in `/etc/fstab` with `nofail,_netdev,x-systemd.automount` options
- PUID/GUID must match TrueNAS dataset permissions (configured in `roles/media_vm/defaults/main.yml`)
- Outbound torrent traffic is routed through the `gluetun` container (Mullvad provider; account in 1Password)

## Key Commands

### Check running containers

```sh
ssh media  # john@192.168.2.105
docker ps
```

### qBittorrent

- Default user: `admin`
- If auth breaks, check logs for temporary password: `docker compose logs qbittorrent`
- Template config file contains the correct password hash

### Check NFS mount health

```sh
mount | grep nfs
findmnt -t nfs4
ls /mnt/nfs/media/
```

## Prometheus Metrics

```promql
# Container status (running = 1, stopped = 0) — check via cAdvisor or Docker metrics
# Filter for media VM containers
container_last_seen{instance=~".*media.*", name=~"qbittorrent|gluetun|sonarr|radarr|prowlarr"}

# NFS mount health via share drive probe
share_drive_probe_success{instance=~".*media.*"}

# Network I/O (useful for checking VPN throughput)
rate(node_network_receive_bytes_total{instance=~".*media.*", device!="lo"}[5m])
rate(node_network_transmit_bytes_total{instance=~".*media.*", device!="lo"}[5m])
```

## Troubleshooting

### qBittorrent can't access media files

1. Check NFS mount: `mount | grep nfs` and `findmnt -t nfs4`
2. Verify PUID/GUID in docker compose matches TrueNAS user
3. Check TrueNAS NFS share is enabled (see truenas-storage runbook for API toggle)
4. If mount is stale, see nfs-smb-shares runbook for recovery steps

### VPN not connected

1. Check Gluetun container logs: `docker compose logs gluetun`
2. Test VPN connectivity: `docker exec gluetun wget -qO- https://am.i.mullvad.net/connected`
3. Verify the Mullvad account is active (check 1Password for account number)
4. Check if the VPN container is healthy: `docker inspect --format='{{.State.Health.Status}}' gluetun`

## Related Services

- TrueNAS (NFS shares for media storage)
- NFS/SMB shares runbook (mount health and recovery)
- jellyfin_lxc at 192.168.2.110 (Jellyfin runs there, not on this VM — public access via Cloudflare Tunnel + Traefik)
- Quiet hours (currently disabled — was used to pause containers at night)
