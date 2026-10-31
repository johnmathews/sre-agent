# Quiet Hours (Docker Sleep System)

> **DISABLED — DO NOT RECOMMEND.** This system is fully disabled and all timers are inactive. Do not suggest quiet-hours
> actions, commands, or troubleshooting steps to users. This runbook exists only as a historical reference.

## Status: CURRENTLY DISABLED

Quiet hours is not currently in use. It was developed to investigate reliable HDD spindown on TrueNAS during nighttime
hours but added complexity without sufficient benefit. The system is fully implemented and tested but all timers are
disabled.

It was disabled because the added operational complexity (share toggling, container pausing, Uptime Kuma integration,
SABnzbd detection) outweighed the benefit of HDD spindown. The homelab runs fine with HDDs spinning continuously.

This runbook is retained for reference in case quiet hours is re-enabled.

## Purpose

Pauses or stops Docker containers during nighttime hours to prevent HDD wakeup and allow disk spindown on TrueNAS. Also
disables NFS/SMB shares during quiet hours to prevent any client I/O. Runs on systemd timers.

## Architecture

- Systemd timers trigger four operations: pause, unpause, stop, start
- Uses systemd templating: `docker-sleep@<operation>.timer` triggers `docker-sleep@<operation>.service`
- `docker-sleep.sh` performs the actual container operations
- The Uptime Kuma integration was removed on 2026-10-31 with Kuma's retirement (Gatus replaces Kuma)
- Integrates with TrueNAS API to disable/enable NFS and SMB shares
- SABnzbd plugin detects active downloads to avoid interrupting them

## Schedule (when enabled)

- **Start quiet hours:** 23:55
- **End quiet hours:** 08:45

Variables in `roles/sleep_hours/defaults/main.yml` or host_vars.

## Key Commands

### Manually trigger operations

```sh
sudo systemctl start docker-sleep@pause.service
sudo systemctl start docker-sleep@unpause.service
sudo systemctl start docker-sleep@stop.service
sudo systemctl start docker-sleep@start.service
```

**Important:** Do not run the shell script directly — it requires environment variables supplied by the systemd service
unit.

### Check timer status

```sh
systemctl status docker-sleep@pause.timer
systemctl status docker-sleep@unpause.timer
TZ=Europe/Amsterdam systemctl list-timers --all
```

### Deploy via Ansible

```sh
make site tags=sleep
make <host> tags=sleep
```

## File Locations

- Script: `/usr/local/bin/docker-sleep.sh`
- TrueNAS shares script: `/usr/local/bin/truenas-shares.sh`
- Container pause list: `/etc/sleep-hours/containers.pause.list`
- Container stop list: `/etc/sleep-hours/containers.stop.list`
- Timer units: `/etc/systemd/system/docker-sleep@*.timer`
- Service template: `/etc/systemd/system/docker-sleep@.service`

## Related Services

- TrueNAS (NFS/SMB share control)
- SABnzbd (busy detection plugin)
