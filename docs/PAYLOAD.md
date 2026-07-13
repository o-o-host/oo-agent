# oo-agent → server payload contract

Everything the backend must accept, store and surface. One JSON object
per push, POSTed to `POST <server>/agent/v1/metrics` with:

```
Authorization: Bearer <agent token>
Content-Type: application/json
Content-Encoding: gzip
User-Agent: oo-agent/<version>
```

Response handling on the agent side: `2xx` = accepted; any `4xx` =
permanent rejection (payload is dropped, `401/403` means the operator
must re-enroll); `5xx` / network error = transient (payload is parked
in the on-disk queue and re-sent oldest-first, so the server MUST
tolerate out-of-order and late arrivals — always trust the payload's
own `ts`, never the arrival time).

## Cadence

| What | Interval | Notes |
|---|---|---|
| Base metrics + sensors | 60 s | `interval` in `[agent]`, per-collector override |
| Inventory collectors | 600 s | `inventory_interval`; fs/gpu inventory rides along with their metrics every push |
| `docker_states` | 60 s | state detection is a base-cadence collector by design |
| `cron_jobs`, `hardware`, `smart`, `dockers` | 600 s | |

A push contains whatever collectors were due that tick, so **any
top-level section and any individual key may be absent**. Merge by
key, never expect the full set in one push. `inventory` keys replace
the previous value wholesale (no diffing on the agent side).

## Top-level envelope

```jsonc
{
  "agent": {
    "version": "0.1.0",
    "fingerprint": "a3f9...",     // stable machine id — the identity key
    "hostname": "web-01",
    "os": "linux",                // "linux" | "windows" | other sys.platform
    "os_version": "Linux-6.8.0-...-x86_64-with-glibc2.39",
    "arch": "x86_64"
  },
  "ts": 1783900000,               // unix seconds, agent clock
  "metrics": { "<key>": <number | array> },
  "sensors": [ { ... } ],         // see Sensors
  "inventory": { ... },           // see Inventory; omitted when empty
  "capabilities": ["system", "fs", ...]   // active collector names
}
```

`capabilities` lists collectors that initialized successfully this
run — use it to decide which UI blocks to render (e.g. no `docker_state`
capability → hide the containers section instead of showing zeros).
Possible names: `system`, `fs`, `diskio`, `net`, `tcpconn`,
`sensors_hwmon`, `sensors_lhm`, `smart`, `docker`, `docker_state`,
`cron`, `hardware`, `rapl`, `gpu_nvidia`, `gpu_amd`, plus any custom
plugin names.

## Metrics (flat numeric channel → Zabbix / time series)

Zabbix-style keys; `[...]` holds the instance dimension. All values are
numbers unless noted.

### Core system (`system`)
| Key | Unit | Notes |
|---|---|---|
| `cpu.util` | % | total |
| `cpu.util.core` | %[] | **array**, one value per logical core |
| `cpu.num` | count | logical cores |
| `cpu.throttle` | 0/1 | throttling detected since last pass |
| `system.load1` / `.load5` / `.load15` | load | Linux only |
| `system.uptime` | s | |
| `mem.pused` | % | |
| `mem.used` / `mem.total` | bytes | used = total − available |
| `swap.pused` / `swap.used` / `swap.total` | % / bytes | |

### Filesystems (`fs`) — per mountpoint
`fs.pused[/]`, `fs.used[/]`, `fs.total[/]` (%, bytes, bytes). The
dimension is the mountpoint string (`/`, `/home`, `C:\`).

### I/O and network
| Key | Unit |
|---|---|
| `diskio.read.kbps` / `diskio.write.kbps` | KiB/s |
| `net.rx.kbps` / `net.tx.kbps` | KiB/s |
| `net.tcp.established` / `net.tcp.synrecv` | count |

### GPU (`gpu_nvidia` / `gpu_amd`) — per GPU index
| Key | Unit | Notes |
|---|---|---|
| `gpu.util[i]` | % | |
| `gpu.mem.used[i]` / `gpu.mem.total[i]` | bytes | |
| `gpu.mem.pused[i]` | % | |
| `gpu.power[i]` | W | |
| `gpu.fan[i]` | % | NVIDIA; absent on fanless/unsupported boards |
| `gpu.throttle[i]` | 0/1 | thermal throttling verdict from the hardware |

### Containers (`docker_state`) — every 60 s
`docker.containers.total`, `.running`, `.exited`, `.restarting`,
`.paused`, `.unhealthy`, `.oom_killed` (counts). `unhealthy` counts
**running** containers with a failing healthcheck only.

### Cron (`cron`)
`cron.jobs` (count), `cron.failed` (count of jobs whose last run
failed — only systemd timers can report this, see below).

### Sensor mirror
Every sensor (next section) is mirrored into the flat channel so
Zabbix can graph/alert without parsing the sensor array:

| Sensor unit | Metric key |
|---|---|
| `C` | `sensor.temp[<sensor id>]` |
| `rpm` | `sensor.fan[<sensor id>]` |
| `V` | `sensor.volt[<sensor id>]` |
| `W` | `sensor.power[<sensor id>]` |
| `A` | `sensor.curr[<sensor id>]` |

(`%` duty-cycle sensors are NOT mirrored — array only.)

### Custom plugins
Drop-in plugin collectors get the `custom.<collector name>.` prefix on
every metric key. Accept unknown `custom.*` keys unconditionally.

## Sensors (rich channel → sensor UI)

```jsonc
{
  "id": "hwmon:k10temp/temp1",   // stable per machine — the join key
  "name": "Tctl",                 // human label
  "kind": "cpu",                  // cpu|core|gpu|vram|disk|ram|board|chipset|fan
  "value": 47.5,
  "unit": "C",                    // C|rpm|V|W|A|%
  "max": 90.0,                    // optional warning bound
  "crit": 100.0,                  // optional hard bound
  "threshold_source": "device"    // present iff max/crit came from the hardware
}
```

- `id` prefixes in the wild: `hwmon:<driver>[-<n>]/<channel>`,
  `lhm:<LibreHardwareMonitor identifier>` (Windows), `gpu:nv<i>/core`,
  `gpu:nv<i>/vram`, `gpu:amd<i>/...`, `smart:/dev/sdX`,
  `rapl:intel-rapl:<socket>[:<sub>]`.
- `unit: "%"`, `kind: "fan"` = fan **duty cycle** (PWM), not RPM — a
  board can expose duty without a tachometer and vice versa.
- Power sensors: RAPL gives per-CPU-package draw (`kind: "cpu"`), hwmon
  `power*` channels give GPU/board rails, LHM gives CPU/GPU package
  power on Windows.

### Overheat evaluation (server side)

The agent ships every threshold the hardware itself publishes —
hwmon `temp*_max`/`temp*_crit`, NVML per-model slowdown/shutdown/VRAM
limits, NVMe warning/critical composite temperature, SATA SCT
operating limits — always tagged `threshold_source: "device"`.
**Device thresholds always win**; they encode the exact model's
passport values and need no lookup.

When a temperature sensor arrives *without* `max`/`crit`, the server
must assign defaults by `kind` (and refine them per model over time —
`inventory.hardware` + `inventory.gpus` + `inventory.smart` give the
exact model strings to key such a dataset on):

| `kind` | warn (`max`) | crit | Notes |
|---|---|---|---|
| `cpu`, `core` | 90 | 100 | Modern desktop/server silicon throttles ~95-100 |
| `gpu` | 83 | 95 | NVIDIA slowdown is typically 83-90 |
| `vram` | 95 | 105 | GDDR6/GDDR6X junction throttles at ~110 |
| `disk` (NVMe) | 70 | 80 | `id` starts with `smart:/dev/nvme`, or check `inventory.smart[].type` |
| `disk` (SATA HDD/SSD) | 50 | 60 | |
| `ram` | 60 | 70 | DIMM thermal sensors where present |
| `board`, `chipset` | 70 | 85 | |

Rules of thumb:
- If only `max` is present, derive `crit = max + 10` (°C).
- Severity: `value >= crit` → critical, `value >= max` → warning; add
  ~3 °C hysteresis on clearing to avoid alert flapping.
- `gpu.throttle[i] = 1` is the hardware's **own** verdict that it is
  thermally throttling — raise an overheat incident on it regardless
  of any threshold math.
- Voltage/current/fan-duty sensors get **no** generic defaults (bounds
  depend on the nominal rail/curve); alert only on device-provided
  limits or user-configured overrides.

## Inventory (structured channel → PostgreSQL)

Each key arrives complete — replace the stored copy for that agent.
`null` field values mean "not supported on this hardware"; keep the
columns nullable.

### `inventory.hardware` (object, 600 s)
```jsonc
{
  "cpuModel": "AMD Ryzen 9 5950X 16-Core Processor",
  "cpuCores": 16, "cpuThreads": 32, "cpuMaxMhz": 5083,
  "ramTotalMb": 64221,
  "boardVendor": "ASUSTeK COMPUTER INC.", "boardModel": "PRIME B450M-A",
  "systemVendor": "...", "systemModel": "...",   // often absent on desktops
  "biosVersion": "4202", "biosDate": "07/12/2023",
  "ramModules": [                                // root/dmidecode (Linux) or CIM (Windows)
    {"sizeMb": 16384, "type": "DDR4", "speedMt": 3200,
     "vendor": "Kingston", "model": "KF3200C16D4/16GX"}
  ],
  "machineType": "physical",     // "physical" | "vm" | "container"
  "hypervisor": "kvm"            // present when detected: kvm|qemu|vmware|...
}
```
`machineType` powers the physical-vs-VPS badge; GPU models come from
`gpus[]`, disk models from `smart[]` — join the three for the
per-model hardware views.

### `inventory.gpus` (array, rides with GPU metrics)
NVIDIA and AMD collectors both append here.
```jsonc
{
  "vendor": "nvidia",            // "nvidia" | "amd"
  "name": "NVIDIA GeForce RTX 3090",
  "load": 63,                    // %
  "memUsedMb": 18432, "memTotalMb": 24576,
  "coreTempC": 67, "vramTempC": 82,   // vramTempC via NVML or the gddr6 PCI-BAR fallback
  "powerW": 287.4,
  "powerLimitW": 350.0,          // NVIDIA only (enforced limit); null/absent on AMD
  "fanPct": 55,
  "thresholds": {                // NVIDIA passport values, may be partial or null
    "slowdown": 93.0, "shutdown": 98.0, "gpu_max": 93.0, "mem_max": 95.0
  }
}
```

### `inventory.smart` (array, 600 s) — physical disks
```jsonc
{
  "dev": "/dev/sda", "type": "ssd",   // ssd|hdd|nvme
  "model": "Samsung SSD 870 EVO 1TB", "serial": "...", "firmware": "...",
  "sizeGb": 1000, "tempC": 34,
  "health": "ok",                     // "ok" | "fail" (SMART overall verdict)
  "powerOnHours": 12345,
  "wearPct": 3,                       // SSD wear, null for HDD
  "reallocated": 0,
  "attributes": [                     // full SMART attribute dump
    {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100,
     "worst": 100, "thresh": 10, "raw": 0, "failing_now": false}
  ]
}
```

### `inventory.disks` (array, rides with fs metrics) — mounted filesystems
```jsonc
{"path": "/", "device": "/dev/sda2", "fstype": "ext4",
 "usedPct": 71.2, "usedGb": 312.4, "totalGb": 439.0}
```

### `inventory.dockers` (array, 600 s) — container resource usage
Heavyweight stats sampled at inventory cadence:
```jsonc
{"name": "web", "image": "nginx:1.25", "state": "running",
 "cpuPct": 2.1, "memMb": 84.3}       // cpuPct/memMb null when stopped
```

### `inventory.docker_states` (array, **60 s**) — container state detector
The near-realtime channel for the containers list and alerting:
```jsonc
{
  "id": "a1b2c3d4e5f6",          // short container id — the join key
  "name": "web", "image": "nginx:1.25",
  "state": "running",            // running|exited|restarting|paused|created|dead
  "health": "healthy",           // healthy|unhealthy|starting|null (no healthcheck)
  "exit_code": null,             // set when not running (137 = OOM/SIGKILL)
  "oom_killed": false,
  "error": null,                 // daemon error string when start failed
  "restart_count": 0,
  "restart_policy": "unless-stopped",   // no|always|unless-stopped|on-failure
  "started_at": 1783890000.5,    // unix seconds, null if never started
  "finished_at": null,           // set when not running
  "uptime_s": 86400              // running containers only
}
```
Suggested alert conditions: `state != "running"` while
`restart_policy != "no"`; `health == "unhealthy"`; `restart_count`
growing between pushes; `oom_killed == true`.

### `inventory.cron_jobs` (array, 600 s) — scheduled job tracking
```jsonc
{
  "id": "9f2c1a...",             // stable hash of (source,user,schedule,command) — the join key
  "source": "user_crontab",      // system_crontab|cron_d|user_crontab|cron_dir|systemd_timer
  "user": "root",
  "name": "backup.py",           // derived display name (or timer unit name)
  "schedule": "30 4 * * *",      // cron spec, @keyword, dir name, or timer calendar spec
  "command": "/usr/bin/python3 /opt/backup.py >> /var/log/backup.log 2>&1",
  "log_file": "/var/log/backup.log",   // first output redirect, null if none
  "enabled": true,               // false = disabled/masked systemd timer
  "next_run": 1783912200.0,      // unix seconds, null when not computable
  "last_run": 1783825800.0,      // null = never observed
  "last_status": "started",      // see below
  "exit_code": null,             // systemd timers only
  "file": "backup"               // cron_d entries only: filename in /etc/cron.d
}
```
**`last_status` semantics — important for the UI:**
- `ok` / `failed` (+ `exit_code`) — systemd timers only; the platform
  records real results there.
- `started` — classic cron: syslog only logs that the job *launched*
  (within the agent's 26 h journal window). Classic cron never records
  exit status anywhere, so the UI must not render `started` as success —
  show it as "ran at <time>", distinct from ok/failed.
- `unknown` — no launch observed in the window (job scheduled less
  often than daily, journald absent, or insufficient permissions).

## Enrollment recap (`/agent/v1/enroll`)

1. `POST /agent/v1/enroll` body `{"agent": {<agent block>}, "enroll_code": "AB7-KQ2-9FD"?}`
   — code present for the code flow, absent for the source-IP claim flow.
   Reply: `{"token": "..."}` (immediate) **or** `{"enroll_id": "...", "status": "pending"}`.
2. Agent polls `GET /agent/v1/enroll/<id>` every 5 s (up to 20 min).
   Reply `{"status": "pending" | "rejected" | "expired"}` or
   `{"status": "approved", "token": "..."}`.
3. The token is stored agent-side with 0600 permissions and used as the
   Bearer for every metrics push. `agent.fingerprint` is the stable
   machine identity — a re-enrolled host keeps its history by
   fingerprint, not by token.

## Storage fan-out recommendation

- `metrics` → Zabbix (trapper items / history) — already flat,
  ready-to-plot values, no server-side preprocessing needed.
- `sensors`, `inventory.*` → PostgreSQL — `sensors` and
  `docker_states` as latest-state upserts keyed by
  `(fingerprint, id)` (plus history tables if trends are wanted);
  other inventory keys as one JSONB blob or normalized tables per key.
- `capabilities`, `agent` → agents table, updated on every push.
