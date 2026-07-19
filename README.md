# oo-agent

Lightweight cross-platform monitoring agent for Linux and Windows —
the data source for the [o-o.host](https://o-o.host) monitoring
platform, [monitor.o-o.host](https://monitor.o-o.host).

It collects system metrics, hardware sensors, disk health, GPU
telemetry, Docker container state and scheduled-job status, and pushes
everything outbound-only to a single HTTPS endpoint
(`POST <server>/agent/v1/metrics`, gzip + Bearer token).

- No inbound connections, no open ports, no public IP required.
- Offline-tolerant: failed pushes are queued on disk and drained
  oldest-first once the backend is reachable again.
- Every collector is isolated — missing hardware or a broken library
  disables that collector only, the agent keeps running.
- Hardware passport thresholds (TjMax, NVML slowdown/shutdown, NVMe
  WCTEMP/CCTEMP, SATA SCT limits, hwmon/RAPL power caps) are read from
  the devices themselves and shipped with the sensor readings.
- Physical vs virtual detection: machine type and hypervisor are
  reported even without root (systemd-detect-virt / DMI / CPUID).
- Custom collectors: drop a plain Python file into the plugins
  directory. See [Write your own sensor plugin](#plugins).

The full wire format — every metric key, sensor schema and inventory
entity — is documented in [docs/PAYLOAD.md](docs/PAYLOAD.md).

## The o-o.host platform

The agent is the on-server half of the o-o.host monitoring stack:

- [monitor.o-o.host](https://monitor.o-o.host) — the panel: server
  dashboards, uptime and SEO checks for sites, hardware sensors with
  per-device thresholds, Docker/cron watching, incidents and
  notifications. Sign up, add a server and the UI hands you the
  one-line install command with an enroll code.
- [o-o.host](https://o-o.host) — hosting: sites from $4.83/mo with
  free SSL.
- Support: [@o_o_host_support_bot](https://t.me/o_o_host_support_bot)
  on Telegram.

The agent works with any backend that implements the
[wire protocol](docs/PAYLOAD.md); nothing in it is hard-wired to
o-o.host.

## Collectors

| Collector | What it ships | Platforms |
|---|---|---|
| `system` | CPU, load, memory, swap, uptime, process count | Linux, Windows |
| `fs` | Per-filesystem usage + disk inventory | Linux, Windows |
| `diskio` | Per-device read/write throughput and IOPS | Linux, Windows |
| `net` | Per-interface traffic, packets, errors | Linux, Windows |
| `tcpconn` | TCP connection counts by state | Linux, Windows |
| `sensors_hwmon` | Temperatures, fan RPM/PWM, voltages, currents, power rails from `/sys/class/hwmon` | Linux |
| `sensors_lhm` | Same sensor set via LibreHardwareMonitor | Windows |
| `rapl` | Per-CPU-package power draw (Intel/AMD RAPL) | Linux |
| `smart` | Disk health, wear, temperatures via `smartctl` | Linux, Windows |
| `gpu_nvidia` | Utilization, VRAM, temps, power + limit, fan, throttle flag, per-model thermal thresholds (NVML); GDDR6/6X VRAM-temperature fallback for GeForce boards via the external `gddr6` tool | Linux, Windows |
| `gpu_amd` | Utilization, VRAM, temps, power via sysfs/amdgpu | Linux |
| `docker` | Per-container CPU/memory/network stats | Linux |
| `docker_state` | Container inventory: state, health, restarts, exit codes, image | Linux |
| `cron` | Scheduled jobs from crontabs + systemd timers, with last-run status | Linux |
| `hardware` | CPU/board/BIOS model, RAM modules (dmidecode/CIM), machine type + hypervisor | Linux, Windows |

## Requirements

- **Linux**: Python 3.10+. Optional external tools, used when present:
  `smartmontools` (SMART), `dmidecode` (RAM modules),
  [`gddr6`](https://github.com/olealgoritme/gddr6) (GeForce VRAM
  temperature; root only). Run as root to see everything.
- **Windows**: none for the packaged installer — it bundles Python,
  LibreHardwareMonitor and `smartctl`. For source runs: Python 3.10+.

## Quick start (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/oo-agent --once --dump        # single collection pass, JSON to stdout
.venv/bin/oo-agent --list-collectors    # show discovered collectors and status
sudo .venv/bin/oo-agent --once --dump   # root pass: SMART, dmidecode, gddr6
```

## Installation

### Linux

```bash
sudo deploy/linux/install.sh --server https://monitor.o-o.host/api --enroll
```

The installer creates a venv under `/opt/oo-agent`, writes
`/etc/oo-agent/agent.ini`, installs the `oo-agent.service` systemd
unit and starts it. Flags:

- `--server URL` — backend base URL, written into `agent.ini`
- `--enroll` — run the enroll flow after install (IP-claim)
- `--enroll CODE` — enroll with a one-time code from the UI
- `--force` — re-enroll over an existing install (discards the old token)
- `--no-start` — install everything but do not enable/start the unit

### Windows

Build the standalone executables and the installer (PyInstaller +
Inno Setup):

```powershell
deploy\windows\build.ps1     # produces dist\oo-agent.exe + setup wizard
```

`deploy/windows/oo-agent.iss` packages the agent as a Windows service
with LibreHardwareMonitor bundled for sensor access.

## Update and uninstall

```bash
sudo oo-agent update       # self-update from the backend's /dl/ manifest
sudo oo-agent update --check
sudo oo-agent uninstall    # remove service, config, token and venv
```

`oo-agent update` fetches `<site>/dl/agent.json`, verifies the tarball
checksum, upgrades the venv in place and restarts the service — the
token and config are kept. The backend can also trigger the same
update remotely by replying `{"update": true}` to a metrics push
(the "Update agent" button in the UI). On Windows use
`deploy\windows\uninstall.ps1` from an elevated PowerShell.

## Configuration

INI file; search order: `--config PATH`, then `/etc/oo-agent/agent.ini`
(Linux) or `%ProgramData%\oo-agent\agent.ini` (Windows). See
[`agent.ini.example`](agent.ini.example) for all options.

```ini
[agent]
interval = 60              ; base metrics cadence, seconds
inventory_interval = 600   ; inventory snapshot cadence, seconds

[transport]
server = https://monitor.o-o.host/api
; token is written by the enroll flow into token_file (mode 0600)

[collector:docker]
enabled = false            ; per-collector opt-out / interval override
```

## Enrollment

Two ways to obtain the agent token:

```bash
oo-agent --enroll ABC123    # one-time code shown in the monitoring UI
oo-agent --enroll           # IP-claim: request appears in the UI,
                            # the agent polls until an operator approves it
```

The received token is stored in `token_file` and used as the Bearer
token on every push.

## Plugins

Custom collectors are plain `.py` files in the plugins directory
(`/etc/oo-agent/plugins` on Linux, `%ProgramData%\oo-agent\plugins`
on Windows). Metric keys get the `custom.<plugin name>.` prefix
automatically. Minimal example:

```python
from oo_agent.plugin import Collector

class MyExoticSensor(Collector):
    name = "my_exotic"          # metric prefix: custom.my_exotic.*
    interval = 60               # seconds, overridable from agent.ini

    def collect(self):
        return {
            "metrics": {"temp_board2": 47.5},
            "sensors": [{"name": "Board #2", "kind": "board",
                         "value": 47.5, "unit": "C", "max": 80}],
        }
```

A fuller collector can gate itself on available hardware, read its
`[collector:<name>]` config section and ship inventory snapshots:

```python
import os
from oo_agent.plugin import Collector

class UpsCollector(Collector):
    name = "ups"
    inventory = True            # runs at inventory_interval cadence

    def available(self):
        # Called once at startup; False disables the collector quietly.
        return os.path.exists("/run/nut/upsmon.pid")

    def collect(self):
        charge, load_w, model = 98.0, 240.0, "APC Smart-UPS 1500"
        limit = float(self.config.get("load_limit_w", 900))
        return {
            "metrics": {"charge_pct": charge, "load_w": load_w},
            "sensors": [{"id": "ups:load", "name": f"{model} load",
                         "kind": "board", "value": load_w, "unit": "W",
                         "max": limit, "threshold_source": "device"}],
            "inventory": {"ups": [{"model": model, "chargePct": charge}]},
        }
```

A plugin that raises an exception is disabled until the next restart;
the agent itself keeps running.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
