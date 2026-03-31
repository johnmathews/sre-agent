# Disk Management

## Purpose

Monitor and manage disks across the homelab — Proxmox host SSDs, TrueNAS HDDs, and VM/LXC virtual disks. Includes SMART
monitoring, ZFS pool health, and disk replacement procedures.

## Key Commands

### Check disk usage (any host)

```sh
df -h
lsblk
```

### SMART health check

```sh
# Check SMART status
smartctl -a /dev/sdX

# Run short self-test
smartctl -t short /dev/sdX

# View test results
smartctl -l selftest /dev/sdX
```

### ZFS pool health (TrueNAS)

```sh
zpool status
zpool list
zfs list -o name,used,avail,refer,mountpoint
```

### Proxmox storage overview

```sh
pvesm status
```

## Prometheus Metrics

Key disk metrics to query:

```promql
# Disk space usage percentage
100 - (node_filesystem_avail_bytes / node_filesystem_size_bytes * 100)

# Disk I/O rate
rate(node_disk_read_bytes_total[5m])
rate(node_disk_written_bytes_total[5m])

# Disk I/O wait time
rate(node_disk_io_time_seconds_total[5m])
```

## HDD Spinup / Spindown Detection

TrueNAS HDDs spin down when idle and spin up on access. There are two complementary approaches to detect spinup/spindown:
the `disk_power_state` metric (direct power state) and `node_disk_io_time_seconds_total` (indirect via IO activity).

### Primary: disk_power_state metric

The `disk-status-exporter` on TrueNAS exports `disk_power_state` as a numeric gauge with labels `device_id`, `device`,
`type` (always `hdd` — SSDs are filtered by the exporter), and `pool`.

**Power state values:**

| Value | State            | Meaning                                                 |
| ----- | ---------------- | ------------------------------------------------------- |
| `-2`  | `error`          | smartctl returned an error                              |
| `-1`  | `unknown`        | state could not be determined, or device is in cooldown |
| `0`   | `standby`        | drive is spun down (platters stopped)                   |
| `1`   | `idle`           | generic idle (not further classified by firmware)       |
| `2`   | `active_or_idle` | drive is active or idle (smartctl cannot distinguish)   |
| `3`   | `idle_a`         | ACS idle_a (shallow idle, fast recovery)                |
| `4`   | `idle_b`         | ACS idle_b (heads unloaded)                             |
| `5`   | `idle_c`         | ACS idle_c (heads unloaded, lower power)                |
| `6`   | `active`         | drive is actively performing I/O                        |
| `7`   | `sleep`          | deepest power-saving mode (requires reset to wake)      |

Classification: values 1-6 = spun up/active, 0 and 7 = spun down, -2 and -1 = error/unknown.

The exporter also provides:

- `disk_power_state_string` — always 1, with a `state` label for human-readable display
- `disk_info` — always 1, static metadata (join on `device_id`)
- `disk_exporter_scan_seconds` — scrape duration
- `disk_exporter_devices_total` — device counts by category

```promql
# Current power state of all HDDs
disk_power_state{type="hdd"}

# Human-readable state via label
disk_power_state_string == 1

# Disks currently in standby
disk_power_state == 0

# Disks that are active or spinning (values 1-6)
disk_power_state >= 1 and disk_power_state <= 6

# State changes in the last hour
changes(disk_power_state{type="hdd"}[1h])
```

**Note:** The `hdd_power_status` composite tool handles all querying, cross-referencing, and transition detection
automatically. Use it instead of manually chaining Prometheus queries.

### Cross-referencing disk identity

The `device_id` labels in `disk_power_state` are opaque (e.g. `wwn-0x5000c500eb02b449`). The `hdd_power_status` tool
automatically cross-references these with TrueNAS disk inventory to report human-readable disk names (model, size, serial
number).

### Secondary: node_disk_io_time_seconds_total

As a fallback (if `disk_power_state` is unavailable), use IO time from node_exporter. This counter only increases when a
disk is doing IO — a spun-down disk shows zero increase.

```promql
# IO time increase per disk in the last 1 hour (non-zero = disk was active)
increase(node_disk_io_time_seconds_total{instance=~".*truenas.*"}[1h])

# Narrow to just HDDs by device name (cross-reference with truenas_list_disks)
increase(node_disk_io_time_seconds_total{instance=~".*truenas.*", device=~"sd[c-h]"}[1h])

# Shorter windows to find the MOST recent spinup
increase(node_disk_io_time_seconds_total{instance=~".*truenas.*", device=~"sd[c-h]"}[5m])
```

### Recommended agent strategy

For any HDD power state question (spinup, spindown, state changes), use the `hdd_power_status` tool. It accepts optional
`duration` (default `"24h"`, e.g. `"12h"`, `"3d"`, `"1w"`) and `pool` filter (e.g. `"tank"`, `"backup"`). It handles all
the complexity automatically:

- Queries current power state from Prometheus
- Cross-references device IDs with TrueNAS disk inventory for human-readable names
- Enriches disk entries with pool assignments from `/pool` topology (since `/disk` may return pool as null)
- Reports change counts and time-in-state percentages for the requested duration
- Uses progressive `changes()` widening (1h→6h→24h→7d) to find recent transitions
- Pinpoints exact transition timestamps via range queries

### Attributing spinup causes

A disk spinup is **confirmed** caused by a process only if there is direct evidence that the process performed disk I/O
(e.g. a block device read/write in logs, an NFS mount access, or a logged smartctl invocation that did not use
`-n standby`). Temporal proximity alone — e.g. "a smartctl scan took 10s and the disk spun up during that window" — is
**not proof**. The scan duration increase could be a _consequence_ of the spinup (reading a waking disk is slow), not its
cause.

When reporting spinup causes, always state whether the attribution is:

- **Confirmed** — direct evidence of disk I/O by the identified process
- **Unconfirmed** — temporal correlation only (events happened close together but no causal link proven)

Common false positives:

- **disk-status-exporter** runs `smartctl -n standby` which explicitly avoids waking sleeping disks. A slow scan
  duration during a spinup window means the disk was already waking, not that the exporter caused it. See
  `runbooks/disk-status-exporter.md` for details.
- **TrueNAS middleware** (TNAUDIT entries) runs REST API calls every ~30s — these are metadata operations that
  typically do not cause disk I/O.

### Related metrics

```promql
# Bytes read/written per disk (confirms what kind of activity)
rate(node_disk_read_bytes_total{instance=~".*truenas.*"}[5m])
rate(node_disk_written_bytes_total{instance=~".*truenas.*"}[5m])

# Disk IO operations (reads + writes)
rate(node_disk_reads_completed_total{instance=~".*truenas.*"}[5m])
rate(node_disk_writes_completed_total{instance=~".*truenas.*"}[5m])
```

## Troubleshooting

### High disk usage alert

1. Identify which file system: check the alert labels for `mountpoint` and `instance`
2. SSH to the host and run `df -h` to confirm
3. Find large files: `du -sh /* 2>/dev/null | sort -rh | head -20`
4. Common culprits: Docker images/volumes, log files, temporary files
5. Clean Docker: `docker system prune -a` (careful — removes unused images)
6. Check log rotation: `journalctl --disk-usage` and `journalctl --vacuum-size=500M`

### SMART warning on a drive

1. Run full SMART check: `smartctl -a /dev/sdX`
2. Look for reallocated sectors, pending sectors, or uncorrectable errors
3. If SMART pre-fail attributes are flagging, plan drive replacement
4. For ZFS: `zpool status` shows if pool is degraded

### Disk I/O bottleneck

1. Check I/O wait: `iostat -x 1 5`
2. Identify processes causing I/O: `iotop -o`
3. For NFS clients: I/O issues may be TrueNAS-side — check NFS server health
4. During quiet hours, HDDs should be spun down — I/O spikes wake them

## Related Services

- TrueNAS (ZFS storage pool management)
- Quiet hours (HDD spindown management)
- Share drive probe (NFS/SMB mount monitoring)
- Prometheus node_exporter (disk metrics)
