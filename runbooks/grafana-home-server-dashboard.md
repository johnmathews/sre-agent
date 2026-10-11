# Grafana "Home Server" Dashboard Reference

## Purpose

This is the primary Grafana dashboard for monitoring the entire homelab. It covers power, storage, compute, networking,
DNS, logs, UPS, temperatures, and container health. **The SRE agent must be able to answer any question this dashboard
can answer.**

Dashboard UID: `dekkfibh9454wb` | Auto-refresh: 10s | Default range: 24h

## Data Sources

| Alias      | Type       | UID              | Used By                 |
| ---------- | ---------- | ---------------- | ----------------------- |
| Prometheus | prometheus | `cethp6u3gnwg0b` | All panels except logs  |
| Loki       | loki       | `aezz63my8sj5sa` | Log Counts by Level     |
| Grafana    | grafana    | `-- Grafana --`  | UPS Alerts, Disk Alerts |

## Template Variables

### `$hostname` (multi-select, default: All)

- Query: `label_values(pve_guest_info, name)`
- Selects VM/LXC guests by name. Filters panels: CPU per VM/LXC, Memory per VM/LXC, CPU per Container, Memory per
  Container, Log Counts, Share Drive State, Container State.

### `$min_log_level` (custom dropdown, default: warn)

- Options: trace, debug, info, warn, error, fatal
- Each option maps to a regex matching that level and above (e.g. warn = `warn|error|fatal`)
- Filters the Log Counts by Level panel

---

## Panels — Detailed Reference

### 1. Tech Shelf Power (row 0, left)

**What it shows:** Real-time and smoothed power consumption of the entire tech shelf (server, NAS, networking gear, UPS)
measured by a Home Assistant smart plug. Normal idle ~70W. Thresholds: 73W=warm, 76W=elevated, 80W+=high.

**Metric:** `homeassistant_sensor_power_w{entity="sensor.tech_shelf_power"}` — raw reading. Use `avg_over_time(...[60m])`
for 1hr, `[3d]` for 3-day, `[14d]` for 14-day moving averages.

**Agent capability:** Answer "what's the current power draw?", "what's the average power this week?", "is power
consumption trending up or down?", "has power usage changed recently?"

---

### 2. HDD State (row 0, right)

**What it shows:** State-timeline of each physical HDD across Proxmox and TrueNAS — spinning vs standby.

**Metric:** `disk_power_state{type="hdd"}` — states: 0=Standby, 1=Idle, 2=Active/Idle, 6=Active, 7=Sleep, -1=Unknown,
-2=Error. Use `hdd_power_status` tool (not raw Prometheus) for best results.

**Disks:** tank pool (2 HDDs on TrueNAS), backup pool (2 HDDs on TrueNAS), Proxmox backup (1 HDD).

**Agent capability:** Answer "are the HDDs spinning?", "how long have the tank disks been idle?", "when did the backup
disks last spin up?", "are any disks in error state?"

---

### 3. AdGuard Queries per Host per Minute (row 8, left)

**What it shows:** DNS query rate per client device, from AdGuard Home DNS.

**Query:**

```promql
sum by (client_name, user) (increase(adguard_queries_details_histogram_count[1m])) > 0
```

**Agent capability:** Answer "which device is making the most DNS queries?", "what's the DNS query rate?", "is any device
generating unusual DNS traffic?"

---

### 4. Total Spindowns (row 8, right — table)

**What it shows:** Table counting HDD spin-down events over 1-day, 7-day, and 30-day windows. Helps track disk wear/power
cycling.

**Queries (all instant, table format):**

```promql
-- 1 day
sum by (disk) (label_join(sum_over_time(disk:spindown_event:1[1d]), "disk", " - ", "hostname", "disk_id")) / 2

-- 7 days
sum by (disk) (label_join(sum_over_time(disk:spindown_event:1[7d]), "disk", " - ", "hostname", "disk_id")) / 2

-- 30 days
sum by (disk) (label_join(sum_over_time(disk:spindown_event:1[30d]), "disk", " - ", "hostname", "disk_id")) / 2
```

**Note:** Uses recording rule `disk:spindown_event:1`. Divided by 2 to correct for duplicate counting.

**Agent capability:** Answer "how many times did the tank disks spin down today/this week/this month?", "which disk has
the most spindown cycles?"

---

### 5. WiFi Clients Download Rate (row 16, left)

**What it shows:** Per-client WiFi download throughput from MikroTik router via MKTXP exporter.

**Query:**

```promql
rate(mktxp_wlan_clients_tx_bytes_total[5m]) > 100
```

Legend: `{{dhcp_name}}` (client hostname from DHCP)

**Agent capability:** Answer "which WiFi device is using the most bandwidth?", "what's the download rate for device X?",
"are any WiFi clients saturating the link?"

---

### 6. Server Network IO/s (row 16, right)

**What it shows:** Network throughput per VM/LXC on Proxmox, combining download and upload.

**Queries:**

```promql
-- Download (uses < 100000 trick to select download metric via join)
(pve_network_download_bytes_per_second * on(id, instance) group_left(name, type) pve_guest_info < 100000)
and on(id, instance) pve_up == 1

-- Upload (uses > 100000 to show as negative/inverted)
(pve_network_upload_bytes_per_second * on(id, instance) group_left(name, type) pve_guest_info > 100000)
and on(id, instance) pve_up == 1
```

**Agent capability:** Answer "which VM/LXC is using the most network bandwidth?", "what's the network throughput for the
media VM?", "is any guest generating unusual network traffic?"

---

### 7. CPU % (row 24, left)

**What it shows:** Proxmox host total CPU usage, IO wait, iGPU utilization, and 15-minute average.

**Queries:** Total CPU = `100 - (avg(rate(node_cpu_seconds_total{mode="idle",hostname="proxmox"}[1m])) * 100)`. IO Wait =
same with `mode="iowait"`. iGPU = `radeontop_gpu_percent`. 15m avg = same idle formula with `[15m]`.

**Agent capability:** Answer "what's the CPU usage on Proxmox?", "is there high IO wait?", "what's the iGPU
utilization?", "what's the 15-minute CPU average?", "is CPU trending up?"

---

### 8. Memory (row 24, right)

**What it shows:** Proxmox host RAM usage in GB with a max capacity line.

**Queries:**

```promql
-- Used RAM (GB)
(node_memory_MemTotal_bytes{hostname="proxmox"} - node_memory_MemAvailable_bytes{hostname="proxmox"}) / 1024 / 1024 / 1024

-- Max line (hardcoded)
60.68
```

**Agent capability:** Answer "how much RAM is in use?", "what percentage of RAM is used?", "how much free memory is
there?". The host has 60.68 GB total RAM.

---

### 9. CPU per VM/LXC (row 32, left)

**What it shows:** CPU usage percentage for each Proxmox guest, relative to its allocated CPU quota.

**Query:**

```promql
clamp_min(
  (pve_cpu_usage_ratio{hostname="proxmox"} * on(id) group_left(name) pve_guest_info{hostname="proxmox", name=~"$hostname"}) * 100,
  0
)
```

Filtered by `$hostname` variable.

**Agent capability:** Answer "which VM is using the most CPU?", "what's the CPU usage for the media VM?", "are any guests
CPU-bound?"

---

### 10. Memory per VM/LXC (row 32, right)

**What it shows:** Memory usage as a percentage of each guest's allocated memory.

**Query:**

```promql
((pve_memory_usage_bytes / pve_memory_size_bytes) * on(id) group_left(name, hostname) pve_guest_info{name=~"$hostname"}) * 100
```

**Agent capability:** Answer "which VM is using the most memory?", "is the media VM running low on RAM?", "which guests
are over-provisioned on memory?"

---

### 11. CPU per Container (row 40, left)

**What it shows:** Docker container CPU usage (top 20), as a percentage of host CPU cores. Data from cAdvisor.

**Query:**

```promql
topk(20,
  (sum by (hostname, container_label_com_docker_compose_service) (
    rate(container_cpu_usage_seconds_total{job="cadvisor", hostname=~"$hostname", container_label_com_docker_compose_service!=""}[2m])
  ) * 100)
  / on (hostname) group_left()
  count by (hostname) (machine_cpu_cores{hostname=~"$hostname"})
)
```

**Agent capability:** Answer "which Docker container is using the most CPU?", "what's the CPU usage of the Immich
container?", "top 5 containers by CPU?"

---

### 12. Memory per Container — % of Host (row 40, right)

**What it shows:** Docker container memory as a percentage of the host machine's total memory.

**Query:**

```promql
100 * sum by (hostname, container_label_com_docker_compose_service) (
  container_memory_working_set_bytes{job="cadvisor", hostname=~"$hostname", container_label_com_docker_compose_service!=""}
) / on (hostname) group_left()
max by (hostname) (machine_memory_bytes{job="cadvisor", hostname=~"$hostname"})
```

**Agent capability:** Answer "which container uses the most memory?", "how much memory is the library-webserver container
using?", "total Docker memory footprint?"

---

### 13. Log Counts by Level (row 48, left)

**What it shows:** Log volume over time grouped by hostname, service, and severity level. Datasource: Loki. Uses log2
scale.

**Query:**

```logql
sum by (hostname, service_name, detected_level) (
  count_over_time(
    {hostname=~"$hostname", service_name!~"^(alloy|cadvisor|node_exporter)$"} | logfmt | detected_level=~"$min_log_level"
  [10m])
)
```

Excludes noisy infrastructure services (alloy, cadvisor, node_exporter). Filtered by `$min_log_level`.

**Agent capability:** Answer "are there any error logs?", "which service is generating the most warnings?", "show me
error log volume over the last hour", "any new error patterns?"

---

### 14. Resource Allocation Summary (row 48, right — table)

**What it shows:** Table of all Proxmox guests with: name, type (qemu/lxc), status (running/stopped), vCPUs, memory size,
memory usage %, disk size, disk usage % (LXC only). All queries use `pve_*` metrics joined by guest `id`.

**Key metrics:** `pve_guest_info` (inventory), `pve_up` (status), `pve_cpu_usage_limit` (vCPUs),
`pve_memory_size_bytes`/`pve_memory_usage_bytes` (memory), `pve_disk_size_bytes`/`pve_disk_usage_bytes` (disk, LXC only).
Thresholds: memory 75%=orange/90%=red, disk 70%=orange/90%=red.

**Agent capability:** Answer "list all VMs and their status", "which VMs are stopped?", "how many vCPUs are allocated
total?", "which guest is using the most memory?", "are any LXC disks near full?"

---

### 15. Share Drive State (row 56, left)

**What it shows:** State-timeline of NFS/SMB share mount probes. Shows whether share drives are accessible from each
client host.

**Query:**

```promql
max(share_drive_probe_state_enriched{hostname=~"$hostname"}) by (hostname, protocol, mount_name)
```

Legend: `{{mount_name}} -> {{hostname}}`

**State mappings:** OK(1)=green, Fail(0)=purple, Error(-1)=red

**Agent capability:** Answer "are all NFS shares accessible?", "is the media share mounted on the media VM?", "any share
drive failures?", "when did a share last fail?"

---

### 16. Fan Speeds (row 56, right)

**What it shows:** IPMI fan RPM readings for CPU and system fans.

**Query:**

```promql
ipmi_fan_speed_rpm
```

Legend: `{{name}}` (CPU0_FAN, SYS_FAN1, SYS_FAN3)

**Agent capability:** Answer "what are the fan speeds?", "is the CPU fan running?", "are fan speeds normal?"

---

### 17. Container State (row 64, left)

**What it shows:** State-timeline of Docker container lifecycle states across all hosts.

**Query:**

```promql
container_state{exported_hostname=~"$hostname"}
```

**State mappings:**

| Value | State      | Color       |
| ----- | ---------- | ----------- |
| 0     | exited     | purple      |
| 1     | running    | green       |
| 2     | paused     | orange      |
| 3     | created    | green       |
| 4     | restarting | dark-orange |
| 5     | dead       | red         |
| 6     | unknown    | default     |

**Agent capability:** Answer "are all containers running?", "has any container restarted recently?", "which containers
are stopped?", "when did container X last restart?"

---

### 18. Temperatures (row 64, right)

**What it shows:** Component temperatures from IPMI sensors and SMART disk temperatures.

**Queries:**

```promql
-- IPMI sensors (excludes noisy/duplicate sensors)
avg_over_time(ipmi_temperature_celsius{name!~"CPU0_DTS|VR_TEMP|B550_FCH_TEMP|VR_TEMP"}[1m])

-- Disk temperatures
smartctl_device_temperature{temperature_type="current"} > 1
```

**Thresholds:** 0°C=green, 40°C=light-orange, 45°C=orange, 60°C=red

**Agent capability:** Answer "what's the CPU temperature?", "are any disks running hot?", "what's the highest temperature
right now?", "are temperatures within safe range?"

---

### 19. Prometheus Data Size (row 72, left)

**What it shows:** Prometheus TSDB storage usage vs configured retention limit.

**Queries:**

```promql
prometheus_tsdb_storage_blocks_bytes{}    -- current size
prometheus_tsdb_retention_limit_bytes{}   -- configured limit
```

**Agent capability:** Answer "how much disk is Prometheus using?", "is Prometheus approaching its storage limit?",
"what's the retention limit?"

---

### 20. UPS Time Online (row 72, stat)

**What it shows:** Days since the last power outage (time since UPS was last on battery).

**Query:**

```promql
clamp_min(
  (time() - max_over_time(
    (timestamp(network_ups_tools_ups_status{flag="OB"})
      and on(instance) (network_ups_tools_ups_status{flag="OB"} == 1)
    )[60d:]
  )) / 86400
, 0)
```

**Agent capability:** Answer "when was the last power outage?", "how long has the UPS been on mains power?"

---

### 21. UPS Charge (row 72, gauge)

**What it shows:** Current UPS battery charge percentage.

**Query:** `network_ups_tools_battery_charge`

**Thresholds:** 0-25% red, 25-50% orange, 50-75% yellow, 75-100% green

**Agent capability:** Answer "what's the UPS battery charge?"

---

### 22. UPS Runtime (row 72, gauge)

**What it shows:** Estimated UPS battery runtime in minutes.

**Query:** `network_ups_tools_battery_runtime / 60`

**Thresholds:** 0-12min red, 12-22min orange, 22-32min yellow, 32-55min green

**Agent capability:** Answer "how long would the UPS last on battery?"

---

### 23. UPS Load (row 76, gauge)

**What it shows:** UPS load as a percentage of capacity.

**Query:** `network_ups_tools_ups_load`

**Thresholds:** 0-25% green, 25-50% yellow, 50-75% orange, 75-100% red

**Agent capability:** Answer "what's the UPS load?", "how much UPS capacity is being used?"

---

### 24. UPS Alerts (row 76, alert list)

**What it shows:** Grafana alert rules matching "ups" — shows firing, pending, and inactive UPS-related alerts.

**Agent capability:** Answer "are there any UPS alerts?", "is the UPS healthy?"

---

### 25. Duration of Prometheus Data (row 80, stat)

**What it shows:** Total time span of Prometheus data retention in days.

**Query:**

```promql
(prometheus_tsdb_head_max_time_seconds - prometheus_tsdb_lowest_timestamp_seconds) / 86400
```

**Agent capability:** Answer "how far back does Prometheus data go?", "what's the data retention period?"

---

### 26. Disk Alerts (row 80, alert list)

**What it shows:** Grafana alert rules matching "disk" — SMART warnings, pool degradation, disk full alerts.

**Agent capability:** Answer "are there any disk alerts?", "are all disks healthy?"

---

### 27. Interface Transmit Rate (row 84, right)

**What it shows:** MikroTik router interface throughput (upload and download bytes/sec) for all physical and wireless
interfaces.

**Queries:**

```promql
mktxp_interface_upload_bytes_per_second
mktxp_interface_download_bytes_per_second
```

Also has hidden rate-based queries for specific interfaces (ether2, ether3, wifi1, wifi2).

**Agent capability:** Answer "what's the internet bandwidth usage?", "which interface has the most traffic?", "what's the
upload/download rate on ether2?"

---

## SRE Agent Capability Scope

The agent MUST be able to answer questions in all of these domains with the same accuracy as reading the dashboard:

### Power & UPS

- Current power draw and trends (1hr, 3day, 14day averages)
- UPS status: charge, runtime, load, time since last outage
- UPS alerts

### Storage & Disks

- HDD power states (spinning/standby/error)
- Spindown event counts (1d/7d/30d)
- Disk temperatures (SMART)
- Disk alerts
- Share drive mount status (NFS/SMB probes)

### Compute — Host Level

- Proxmox CPU usage (total, IO wait, 15min avg)
- Proxmox RAM usage (used GB, percentage, total capacity = 60.68 GB)
- iGPU utilization (radeontop)
- Fan speeds (IPMI)
- Component temperatures (IPMI)

### Compute — Guest Level (VM/LXC)

- Per-guest CPU usage (% of allocated quota)
- Per-guest memory usage (% of allocation)
- Guest inventory: name, type, status, vCPUs, memory, disk

### Compute — Container Level (Docker)

- Per-container CPU usage (% of host cores, top 20)
- Per-container memory usage (% of host RAM)
- Container lifecycle state (running/exited/restarting/dead)

### Networking

- Per-VM/LXC network throughput (download + upload)
- WiFi client download rates (per device by DHCP name)
- MikroTik router interface throughput (all interfaces)

### DNS

- AdGuard query rate per client per minute
- Identify noisy DNS clients

### Logs

- Log volume by hostname, service, and severity level
- Filterable by minimum log level
- Excludes infrastructure noise (alloy, cadvisor, node_exporter)

### Observability Self-Monitoring

- Prometheus TSDB storage size vs retention limit
- Prometheus data retention duration (days)

### Key Metrics — Infrastructure & Power

| Metric                             | Source            | Domain          |
| ---------------------------------- | ----------------- | --------------- |
| `homeassistant_sensor_power_w`     | Home Assistant    | Power           |
| `network_ups_tools_*`              | NUT exporter      | UPS             |
| `ipmi_fan_speed_rpm`               | IPMI exporter     | Fans            |
| `ipmi_temperature_celsius`         | IPMI exporter     | Temperatures    |
| `smartctl_device_temperature`      | smartctl exporter | Disk temps      |
| `disk_power_state`                 | custom exporter   | Disk state      |
| `disk:spindown_event:1`            | recording rule    | Disk spindowns  |
| `share_drive_probe_state_enriched` | custom exporter   | Share mounts    |
| `prometheus_tsdb_*`                | Prometheus        | Self-monitoring |

### Key Metrics — Compute & Networking

| Metric                                    | Source             | Domain              |
| ----------------------------------------- | ------------------ | ------------------- |
| `node_cpu_seconds_total`                  | node_exporter      | Host CPU            |
| `node_memory_*_bytes`                     | node_exporter      | Host memory         |
| `radeontop_gpu_percent`                   | radeontop exporter | iGPU                |
| `pve_cpu_usage_ratio`                     | PVE exporter       | Guest CPU           |
| `pve_memory_*_bytes`                      | PVE exporter       | Guest memory        |
| `pve_guest_info` / `pve_up`               | PVE exporter       | Guest inventory     |
| `pve_cpu_usage_limit`                     | PVE exporter       | Guest vCPUs         |
| `pve_disk_*_bytes`                        | PVE exporter       | Guest disk          |
| `pve_network_*_bytes_per_second`          | PVE exporter       | Guest network       |
| `container_cpu_usage_seconds_total`       | cAdvisor           | Container CPU       |
| `container_memory_working_set_bytes`      | cAdvisor           | Container memory    |
| `container_state`                         | custom exporter    | Container lifecycle |
| `adguard_queries_details_histogram_count` | AdGuard exporter   | DNS                 |
| `mktxp_wlan_clients_tx_bytes_total`       | MKTXP              | WiFi                |
| `mktxp_interface_*_bytes_per_second`      | MKTXP              | Router interfaces   |
