# Proxmox Virtualization

## Overview

The homelab runs on a single Proxmox VE node hosting ~20 guests (mix of VMs and LXC containers).

## Resource Allocation Behavior

### Stopped vs Running Guests

**Stopped guests consume zero host CPU and zero host RAM.** The vCPU count and RAM size shown in a stopped guest's configuration are just settings — they are only claimed from the host when the guest is started. While stopped, a guest uses only disk space for its virtual disks.

- **Running guest:** actively consuming host CPU and RAM up to its configured limits
- **Stopped guest:** consumes only disk space; CPU and RAM allocations are not reserved

Do not describe stopped guests as "reserving" or "consuming" RAM or CPU. They are idle definitions, not active reservations.

### Memory Allocation

Proxmox does not reserve memory for stopped guests. When a guest starts, it requests memory from the host. If the host lacks sufficient free memory, the guest will fail to start.

- LXC containers: memory usage can fluctuate; the `maxmem` setting is a hard ceiling
- QEMU VMs: memory is allocated at boot (unless ballooning is enabled)

### CPU Allocation

vCPU counts are not pinned 1:1 to physical cores by default. Multiple guests can share the same physical cores. The total vCPU count across all running guests can safely exceed the host's physical core count (overcommit), though heavy overcommit increases scheduling latency.

## Guest Types

| Type | Technology | Use Cases |
|------|-----------|-----------|
| VM (qemu) | Full virtualization | TrueNAS, Home Assistant, anything needing a full kernel |
| CT (lxc) | OS-level containers | Lightweight services, single-purpose workloads |

LXC containers have lower overhead than VMs but share the host kernel. VMs provide full isolation.

## Key Metrics

| Metric | Source | Meaning |
|--------|--------|---------|
| `pve_up` | PVE exporter | 1 if guest is running, 0 if stopped |
| `pve_cpu_usage_ratio` | PVE exporter | CPU usage as fraction of allocated vCPUs |
| `pve_memory_usage_bytes` | PVE exporter | Current RAM usage (running guests only) |
| `pve_memory_size_bytes` | PVE exporter | Configured RAM limit |
| `pve_guest_info` | PVE exporter | Guest metadata (name, type, status) |

## Common Questions

**Q: Are stopped guests wasting resources?**
A: No. They use only disk space. Their CPU and RAM allocations are inactive until started.

**Q: Can I start all guests at once?**
A: Only if the host has enough free RAM for all of them. Check `node_memory_MemAvailable_bytes` on the Proxmox host before starting multiple large guests.

**Q: Which guests use the most resources?**
A: Query `pve_memory_usage_bytes` and `pve_cpu_usage_ratio` for running guests. Stopped guests use none.
