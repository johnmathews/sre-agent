# Disk Status Exporter

The `disk-status-exporter` is a Prometheus exporter that monitors the power state of physical hard drives on TrueNAS. It
runs as a Docker container on the TrueNAS host, exposing metrics at port 9635.

Source code: https://github.com/johnmathews/disk_status_exporter

## Key Fact: smartctl Will NOT Wake Sleeping Disks

The exporter uses `smartctl -d sat,12 -n standby -i <dev>` to probe each HDD. Three mechanisms prevent waking sleeping
drives:

1. **`-n standby`** (the primary mechanism): tells smartctl "if the device is in STANDBY or SLEEP mode, do NOT send any
   command that would wake it — just print the power state and exit."
2. **`-d sat,12`** (SATA passthrough): tells smartctl the exact device type, bypassing autodetection probes that can
   wake a sleeping drive.
3. **`-i`** (info-only): requests only device identification, NOT SMART data, self-test logs, or error logs (which would
   require the drive to be spun up).

**This means the disk-status-exporter cannot be the cause of a disk spinup.** If a disk spins up while the exporter is
running, the exporter is observing the spinup, not causing it.

## Pull-Based Model

The exporter has no internal polling loop. It probes disks on-demand when Prometheus scrapes the `/metrics` endpoint. The
scrape frequency is controlled by Prometheus's `scrape_interval` configuration for this target, not by the exporter.

## Timeout Cooldown

If a smartctl call times out (10-second limit), the device enters a cooldown period (`COOLDOWN_SECONDS`, default 300s /
5 minutes). During cooldown, the device is skipped entirely — no subprocess is spawned. This prevents hammering a device
that might be in the process of spinning up.

## Interpreting Scan Duration

The `disk_exporter_scan_seconds` metric measures how long the entire scrape took. A high scan duration (e.g. 10+ seconds)
during a spinup window does **not** mean the exporter caused the spinup. It means:

- The exporter attempted to read a disk that was already in the process of waking
- smartctl had to wait for the disk to become responsive (even with `-n standby`, a disk that is mid-spinup may not
  respond immediately)
- The slow scan is a **consequence** of the spinup, not its cause

## Exported Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `disk_power_state` | Gauge | Numeric power state code (-2 to 7) |
| `disk_power_state_string` | Gauge | Always 1; carries human-readable state as `state` label |
| `disk_info` | Gauge | Always 1; static disk metadata for label joins |
| `disk_exporter_scan_seconds` | Gauge | Duration of the last scrape in seconds |
| `disk_exporter_devices_total` | Gauge | Device counts by category |

### Power State Values

| Value | State | Description |
|-------|-------|-------------|
| -2 | error | smartctl error |
| -1 | unknown | Could not determine / in cooldown |
| 0 | standby | Spun down (platters stopped) |
| 1 | idle | Generic idle |
| 2 | active_or_idle | smartctl can't distinguish |
| 3 | idle_a | ACS idle_a (shallow, fast recovery) |
| 4 | idle_b | ACS idle_b (heads unloaded) |
| 5 | idle_c | ACS idle_c (heads unloaded, lower power) |
| 6 | active | Actively performing I/O |
| 7 | sleep | Deepest low-power (requires reset to wake) |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `PROBE_ATTEMPTS` | 1 | Probes per disk per scrape |
| `PROBE_INTERVAL_MS` | 1000 | Delay between probe attempts |
| `MAX_CONCURRENCY` | 8 | Max concurrent smartctl probes |
| `COOLDOWN_SECONDS` | 300 | Skip duration after timeout |

## Filtering

The exporter only reports on rotational (HDD) devices. SSDs, virtual devices (QEMU/virtio), loop devices,
device-mapper, mdraid, and zvols are filtered out automatically.
