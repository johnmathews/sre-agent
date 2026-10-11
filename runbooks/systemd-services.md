# Systemd Service Management

## Purpose

Systemd is the init system and service manager across all VMs and LXCs. Most homelab services run as either Docker
containers (managed by docker compose) or native systemd services (share drive probe, quiet hours timers, NUT).

## Key Concepts

- `.service` — background processes/daemons (most common)
- `.timer` — scheduled jobs (replaces cron)
- `.mount` — mount points
- `.target` — groups of units (like runlevels)

Unit files live in `/etc/systemd/system/` (custom) or `/lib/systemd/system/` (package-provided).

## Key Commands

### Service lifecycle

```sh
sudo systemctl start <service>
sudo systemctl stop <service>
sudo systemctl restart <service>
sudo systemctl reload <service>      # reload config without restart (if supported)
sudo systemctl enable <service>      # start at boot
sudo systemctl disable <service>     # don't start at boot
```

### Inspect a service

```sh
systemctl status <service>           # current state + recent logs
systemctl cat <service>              # view the unit file
systemctl show <service>             # all properties
systemctl list-dependencies <service>
```

### View logs

```sh
journalctl -u <service>              # all logs for a service
journalctl -u <service> -n 50        # last 50 lines
journalctl -u <service> -f           # follow (tail)
journalctl -u <service> -b           # since last boot
journalctl -xeu <service>            # extended with explanations
```

### Timers

```sh
systemctl list-timers --all
systemctl list-timers --all | grep <pattern>
```

### Discovery

```sh
systemctl list-unit-files             # all defined units (running or not)
systemctl list-units                  # all currently loaded units
systemctl list-units --type=service   # only services
```

### After modifying unit files

```sh
sudo systemctl daemon-reload
```

### Remove a service

```sh
sudo systemctl disable <service>
sudo systemctl stop <service>
sudo rm /etc/systemd/system/<service>
sudo systemctl daemon-reload
```

### Check journal disk usage

```sh
journalctl --disk-usage
sudo journalctl --vacuum-size=500M   # trim to 500MB
```

## Homelab-Specific Services

Key systemd services in this homelab:

- `docker-sleep@{pause,unpause,stop,start}` — quiet hours container management
- `share-drive-probe.service` / `.timer` — NFS/SMB mount monitoring
- `nut-server`, `nut-monitor` — UPS monitoring (Proxmox)
- `AdGuardHome` — DNS filtering (AdGuard LXC)
- `unbound` — recursive DNS resolver (AdGuard LXC)
- `tailscaled` — Tailscale VPN daemon (all hosts)

## SSH Host Aliases

Use these to connect to any host by name (configured in `~/.ssh/config`):

| Alias         | IP            | User          | Tailscale alias |
| ------------- | ------------- | ------------- | --------------- |
| `pve`         | 192.168.2.214 | root          | `pvet`          |
| `nas`         | 192.168.2.104 | truenas_admin | `nast`          |
| `infra`       | 192.168.2.106 | john          | `infrat`        |
| `media`       | 192.168.2.105 | john          | `mediat`        |
| `adguard`     | 192.168.2.111 | root          | —               |
| `cloudflared` | 192.168.2.101 | root          | `cloudflaredt`  |
| `traefik`     | 192.168.2.108 | root          | `traefikt`      |
| `prometheus`  | 192.168.2.115 | root          | `prometheust`   |
| `mail`        | 192.168.2.103 | john          | —               |
| `jelly`       | 192.168.2.110 | root          | `jellyt`        |
| `immich`      | 192.168.2.113 | root          | `immicht`       |
| `tube`        | 192.168.2.116 | root          | `tubet`         |
| `documents`   | 192.168.2.117 | root          | `documentst`    |
| `pbs`         | 192.168.2.200 | root          | `pbst`          |
| `key`         | 192.168.2.201 | john          | `keyt`          |
| `router`      | 192.168.2.1   | admin         | —               |

## Related Services

- Docker (most application services run as containers)
- Ansible (deploys and manages systemd units)
