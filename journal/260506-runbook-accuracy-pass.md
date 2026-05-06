# Runbook accuracy pass — May 2026

Cross-referenced all 19 files in `runbooks/` against the home-server ansible
repo (`/Users/john/projects/home-server`, primarily
`proxmox-setup/inventory.ini` and `proxmox-setup/roles/`). Two runbooks had
factually wrong content; the remaining 17 spot-checked clean.

## traefik-reverse-proxy.md

The "Architecture" section claimed Traefik only routed two services (Immich,
Jellyfin). The actual `routers.yml.j2` template defines 13+ routers across
several backends (immich, immich-share, jelly, navidrome, music, timer, docs,
homepage, uptime, speed, sre, stats, plus the local-only `traefik-*` admin
routers). The Jellyfin backend address was also wrong — listed as
`192.168.2.105:8096` (media VM) but the Jellyfin LXC sits at `192.168.2.110`.

Replaced the two-row table with a full router/backend/middleware table and
added a one-line citation pointing back at the routers template so the next
time someone adds a router this file is the obvious place to update.

## media-vm.md

Two mistakes here:

1. The runbook claimed Jellyfin runs on the media VM. It does not — Jellyfin
   has its own LXC (`jellyfin_lxc`, 192.168.2.110), and the media VM's
   docker-compose template confirms there's no `jellyfin` container there.
   The "Jellyfin not reachable from internet" troubleshooting section had a
   `curl http://localhost:8096` step that would fail on the media VM. Removed
   the section, added a banner at the top stating Jellyfin lives elsewhere,
   and updated the Related Services list.
2. The VPN troubleshooting referenced a `mullvad` container, but the actual
   container is `gluetun` running the Mullvad provider. All four `docker`
   commands and the cAdvisor name regex have been corrected.

## Other runbooks

Spot-checked but not edited: cloudflared-tunnel, dns-stack, disk-management,
disk-status-exporter, documentation-server, grafana-home-server-dashboard,
loki-logging, mikrotik-monitoring, nfs-smb-shares, proxmox-virtualization,
quiet-hours (correctly marked disabled), rebuilding-the-vector-store,
request-failure-investigation, systemd-services, tailscale-vpn,
truenas-storage, ups-power. No factual errors found against
`inventory.ini`/role defaults.

## Memory cleanup

Removed the `project_stale_runbooks` memory entry that flagged the Traefik
issue as backlog — it's now resolved.
