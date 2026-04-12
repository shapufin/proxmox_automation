# VMware → Proxmox Migration Tool — Architecture Reference

> Hand this file to any AI or developer to get full context from scratch.

---

## 1. What This Project Does

A **self-hosted Django web application** that migrates virtual machines from VMware (ESXi/vCenter) to Proxmox VE.  
It runs inside a **privileged LXC container** on the Proxmox host (or in Docker).  
It has two migration modes:

| Mode | How it works |
|---|---|
| **VMware Direct** | Connects to ESXi/vCenter via pyvmomi, reads VM specs + exports disks |
| **Local Disk** | Pre-copy VMDK files to a staging directory on the Proxmox host; the app imports them via `qm importdisk` over SSH |

---

## 2. Repository Layout

```
vmware_to_proxmox/           ← Python library (core logic)
│   config.py                  AppConfig dataclass loader (YAML → Python)
│   models.py                  Shared dataclasses: DiskFormat, VmwareVmSpec, MigrationResult, etc.
│   engine.py                  MigrationEngine — orchestrates the full migration workflow
│   proxmox.py                 ProxmoxClient — SSH CLI + REST API + SFTP to Proxmox host
│   vmware.py                  VMwareClient — pyvmomi connection, VM listing, disk export
│   disk.py                    Disk format helpers (qcow2, raw, vmdk detection, archive detection)
│   guest.py                   Guest remediation (fstab rewrite, qemu-agent install)
│   cli.py                     Click CLI entrypoint (non-web usage)
│   logger.py                  Logging setup

webui/                       ← Django project
│   settings.py                Django settings (SQLite path via DJANGO_DB_PATH, env vars)
│   urls.py                    Root URL router (includes dashboard app)
│   dashboard/
│       models.py              MigrationJob, ProxmoxHost, VMwareHost Django models
│       views.py               All HTTP views + JSON API endpoints
│       forms.py               MigrationJobForm, ProxmoxHostForm, VMwareHostForm, etc.
│       services.py            Business logic: execute_job, _engine_from_job, config profiles
│       urls.py                URL patterns for the dashboard app
│       migrations/            Django DB migrations (0001–0006)

templates/dashboard/         ← Django HTML templates
│   base.html                  Nav bar, sidebar (Dashboard / Hosts / Configs / Browse)
│   wizard.html                Multi-step migration wizard (JS state machine)
│   hosts.html                 Host registration management page
│   job_detail.html            Single job log view
│   config_editor.html         YAML config profile editor
│   browse.html                Disk browser

static/
│   dashboard.css              All CSS (dark theme, wizard steps, cards, badges)

entrypoint.sh                ← Container entrypoint: mkdir dirs, exec CMD
config.example.yaml          ← Copy to config.yaml and fill in credentials
docker-compose.yml           ← migrate / web / worker services
Dockerfile                   ← Single image used by all three services
requirements.txt             ← Python dependencies
pyproject.toml               ← Build config (setuptools package discovery)
```

---

## 3. Key Python Files — What Each Does

### `vmware_to_proxmox/config.py`
- Defines `AppConfig`, `VmwareConfig`, `ProxmoxConfig`, `MigrationConfig`, `SshConfig` dataclasses.
- `AppConfig.load(path)` reads `config.yaml` (YAML → dataclasses).
- `_coerce_int` / `_coerce_bool` helpers absorb invalid YAML values with safe defaults.
- **`AppConfig` methods:**
  - `bridge_for_network(name)` — looks up `proxmox.bridge_map`, falls back to `default_bridge`
  - `storage_for_datastore(name)` — looks up `proxmox.datastore_map`, falls back to `default_storage`
  - `target_format()` — resolves disk format from config override or global default
- **Important `ProxmoxConfig` fields:**
  - `bridge_map` — `{"VM Network": "vmbr0", "DMZ": "vmbr1"}` VMware → Proxmox bridge
  - `datastore_map` — `{"datastore1": "local-lvm"}` VMware datastore → Proxmox storage
  - `ssh_*` — SSH credentials to Proxmox host
  - `api_*` — REST API credentials (port 8006)

### `vmware_to_proxmox/proxmox.py`
`ProxmoxClient` — the most critical file. All Proxmox interaction:

| Method | What it does |
|---|---|
| `_run(args)` | CLI command locally OR via SSH (paramiko) |
| `list_storages()` | REST API first, falls back to `pvesm status` via SSH |
| `list_bridges()` | REST API first, falls back to `pvesh get /nodes/{node}/network` |
| `list_remote_dir(path)` | SFTP directory listing on the Proxmox HOST |
| `read_remote_file(path)` | SFTP text-file read for `manifest.json` and `.vmx` parsing |
| `create_vm()` | `qm create` via SSH |
| `import_disk()` | `qm importdisk` via SSH — path must be HOST-absolute. Parses volume ID from output; best-effort regex fallback if the canonical success string changes or is localized |
| `extract_archive()` | Extracts .zip/.7z/.tar.gz on the HOST via SSH (avoids pulling GBs into LXC) |
| `peek_archive()` | Lists archive contents without extraction (`unzip -l`, `7z l`, `tar -tf`) |
| `remove_remote_dir()` | `rm -rf` guarded to safe prefixes only (`/tmp/`, `/var/tmp/`, `/mnt/`, `/data/`) |

**Critical:** App runs inside LXC. `qm`, `pvesh`, `pvesm` are **not** in the LXC. All mutating CLI calls go over SSH to the host.

### `vmware_to_proxmox/engine.py`
`MigrationEngine` — orchestrates the full workflow:
- `inventory()` → `{vmware_vms, proxmox_storages, proxmox_bridges}` for the GUI
- `build_plan()` → fetches VM from VMware, delegates to `_plan_from_vm()` (no duplication)
- `_plan_from_vm()` → resolves per-disk storage via `storage_for_datastore`, per-NIC bridge via `bridge_for_network`
- `migrate_local_disks()` → local VMDK import pipeline
- `migrate_local_disks_or_archive()` → archive-aware entry point (extracts on host, then calls above)
- `migrate_vm()` / `_migrate_vmware()` → VMware direct pipeline

### `webui/dashboard/models.py`
Django models:
- `ProxmoxHost` — registered Proxmox connection profiles (API + SSH credentials, default storage/bridge)
- `VMwareHost` — registered VMware connection profiles
- `MigrationJob` — job record with FKs to `ProxmoxHost` / `VMwareHost`; `disk_storage_map` and `nic_bridge_map` JSONFields for per-disk/per-NIC overrides

### `webui/dashboard/services.py`
- `_engine_from_job(job)` — builds `MigrationEngine` from registered `ProxmoxHost`/`VMwareHost` when set, otherwise falls back to config file
- `execute_job(job)` → calls `_engine_from_job`, dispatches to `migrate_local_disks_or_archive` or `migrate_vm`
- Config profile helpers: `list_config_profiles`, `load_config_profile`, `save_config_profile`

### `webui/dashboard/views.py`
All HTTP handlers:

| View | URL | Purpose |
|---|---|---|
| `wizard` | `GET /` | Migration wizard — passes registered hosts + config profiles |
| `launch_job` | `POST /launch/` | Creates `MigrationJob` with host FKs |
| `run_pending_job` | `POST /jobs/<id>/run/` | Atomic `select_for_update` claim → `execute_job` |
| `job_detail` | `GET /jobs/<id>/` | Job log page |
| `hosts` | `GET /hosts/` | Host registration management |
| `proxmox_status` | `GET /api/proxmox-status/` | Live storages + bridges; 30 s Django cache |
| `vmware_vms` | `GET /api/vmware-vms/` | Live VMware VM list |
| `browse_directory` | `GET /api/browse-directory/?path=` | HOST filesystem via SFTP |
| `vmdk_scan` | `GET /api/vmdk-scan/?path=` | VMDKs in host dir + manifest auto-detect |
| `peek_archive` | `GET /api/peek-archive/?path=` | Archive contents (VMDK list + VMX specs) |
| `test_proxmox_host` | `POST /hosts/proxmox/<id>/test/` | Connectivity test for registered host |
| `test_vmware_host` | `POST /hosts/vmware/<id>/test/` | Connectivity test for registered host |

### `webui/dashboard/forms.py`
- `MigrationJobForm` — wizard form; includes hidden `proxmox_host_id`, `vmware_host_id`, `disk_storage_map`, `nic_bridge_map`
- `ProxmoxHostForm` / `VMwareHostForm` — host registration ModelForms

---

## 4. Frontend (`templates/dashboard/wizard.html`)

Multi-step wizard in vanilla JS — no framework:

| Step | What happens |
|---|---|
| **Step 1 — Target** | Select registered Proxmox host (or config profile fallback); VMware host shown for VMware mode |
| **Step 2 — Source** | Browse HOST filesystem via SFTP modal; scan for VMDKs; auto-peek archives |
| **Step 3 — Options** | Discover live storages/bridges; combobox for storage + bridge; per-disk storage map; per-NIC bridge map |
| **Submit** | POST to `/launch/` with all hidden fields including `proxmox_host_id`, `vmware_host_id`, `disk_storage_map`, `nic_bridge_map` |

**Sidebar nav:** Dashboard → Hosts → Config Profiles → Browse Disks

---

## 5. Docker / Deployment

```
docker-compose.yml — three services, one image (entrypoint.sh + CMD)

migrate:  python manage.py migrate + collectstatic  (exits 0 on success)
web:      gunicorn 2 workers                        (depends_on: migrate)
worker:   python manage.py worker                   (depends_on: migrate)
```

- `entrypoint.sh` — creates runtime directories (`/app/configs`, `/app/data`, `/data/staging`), then `exec "$@"`
- `migrate` service is the **only** place migrations run — no race condition
- All three services share the **same** SQLite file via `DJANGO_DB_PATH=/app/data/db.sqlite3` and the `./data` volume mount

**Environment variables:**

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_DB_PATH` | `<BASE_DIR>/data/db.sqlite3` | SQLite file path (pin to shared volume) |
| `DJANGO_SECRET_KEY` | `change-me` | **Must be changed in production** |
| `DJANGO_ALLOWED_HOSTS` | `*` | Restrict in production |
| `VMWARE_TO_PROXMOX_CONFIG` | `/config/config.yaml` | Path to config.yaml inside container |
| `VMWARE_TO_PROXMOX_CONFIG_DIR` | `/app/configs` | Directory for named config profiles |
| `VMWARE_TO_PROXMOX_STAGE_ROOT` | `/data/staging` | Fallback staging root for relative paths |

**Volume mounts (all services):**
```
./config.yaml → /config/config.yaml   (read-only)
./configs/    → /app/configs
./data/       → /app/data             (contains db.sqlite3)
./staging/    → /data/staging
# Optional — expose Proxmox host storage:
# /var/lib/vz/dump → /mnt/vmware_staging  (read-only)
```

---

## 6. Configuration (`config.yaml`)

```yaml
proxmox:
  node: pve
  default_storage: local-lvm
  default_bridge: vmbr0

  # Network mapping: VMware network name → Proxmox bridge
  bridge_map:
    "VM Network": vmbr0
    "DMZ": vmbr1

  # Datastore mapping: VMware datastore name → Proxmox storage
  datastore_map:
    "datastore1": local-lvm
    "SSD-datastore": nvme-pool

  api_host: 18.0.0.1
  api_user: root@pam
  api_token_name: migration
  api_token_value: "UUID-HERE"
  api_verify_ssl: false

  ssh_enabled: true
  ssh_host: 18.0.0.1
  ssh_username: root
  ssh_password: ""        # or use ssh_private_key

migration:
  dry_run: true           # always start with true
```

**Mapping resolution order:**
1. Per-job override (wizard Step 3 combobox / per-NIC / per-disk rows) stored in `MigrationJob.nic_bridge_map` / `disk_storage_map`
2. `bridge_map` / `datastore_map` in `config.yaml`
3. `default_bridge` / `default_storage`

---

## 7. Dependencies

| Package | Purpose |
|---|---|
| `Django 5.1` | Web framework |
| `gunicorn` | WSGI server |
| `pyvmomi` | VMware vSphere API client |
| `paramiko` | SSH + SFTP to Proxmox host |
| `proxmoxer` | Proxmox REST API client (port 8006) |
| `PyYAML` | Config file parsing |
| `requests` | HTTP (used by proxmoxer) |
| `whitenoise` | Static file serving |
| `click` | CLI entrypoint |

---

## 8. Known Constraints

1. **LXC isolation** — `qm`, `pvesh`, `pvesm` are NOT in the LXC. All mutating CLI calls go over SSH to the Proxmox host.
2. **Host-absolute paths** — `qm importdisk <vmid> <path> <storage>` takes the path as seen by the HOST. The SFTP browser and VMDK scanner always return host-side paths.
3. **SQLite concurrency** — SQLite is used for the job DB. The `migrate` service ensures schema is applied once; SQLite `timeout=20` handles write contention between web + worker. Not suitable for multi-host deployments.
4. **Archive extraction** — archives are extracted on the HOST (not the LXC) via SSH to avoid pulling GBs of disk data. Temp dirs are cleaned after migration.
5. **SDN discovery** — bridges come from the REST API or `pvesh` over SSH. SDN vnets appear if the API user has permissions.
6. **SSH trust model** — Paramiko currently uses `AutoAddPolicy()`, so unknown host keys are accepted on first contact. Production deployments should pin host keys.
7. **Path trust boundary** — the browse/scan APIs forward raw user-supplied paths into SFTP/SSH operations. The client is not a sandbox; access control must happen at the web layer.
8. **Importdisk parsing** — `qm importdisk` output is parsed from a success string plus a regex fallback, so localized or future Proxmox output can break volume-ID detection.
9. **Queue atomicity** — the HTTP run endpoint uses `select_for_update`, but the background worker claims `PENDING` jobs without row-level locking. Multi-worker deployments can double-run the same job.
10. **Idempotency gap** — VM creation, disk import, and attachment are not wrapped in a single transaction. `rollback_on_failure` helps, but crashes after `qm create` can leave orphaned VMs.
11. **VMware return-path bug** — `_migrate_vmware()` currently references `proxmox_name` in its result payload without capturing the return value of `create_vm()`, so a nominally successful import can still fail during result assembly.
12. **Single-node placement** — the engine always targets the configured Proxmox node; there is no HA-group aware scheduler, node scoring, or zone-aware placement.
13. **Storage optimization is naive** — `choose_storage()` is free-space driven only. There is no ZFS/Ceph-aware policy engine, replication-awareness, or performance-class scoring.
14. **Job-scoped state leakage risk** — `MigrationEngine` and `ProxmoxClient` cache SSH/API clients, so any future shared worker or long-lived service instance must treat them as single-job resources and reset/close them after execution to avoid cross-job contamination.

### 8.1 Enterprise expansion opportunities

- **Pre-migration dry run validation** — promote the existing compatibility report into an explicit validation phase that scores snapshot, passthrough, vTPM, storage, and resize risk before any VM is created.
- **Cluster-aware placement and progress telemetry** — add target-node/HA-group/SDN zone/VNet selection plus WebSocket or SSE progress updates so large migrations can be tracked in real time.
- **Storage and guest optimization packs** — add ZFS/Ceph-aware storage policies and optional guest-driver automation such as VirtIO package injection and qemu-guest-agent bootstrapping.

### 8.2 Refactor Roadmap (task-force aligned)

| Workstream | Owner focus | Files | Target changes |
|---|---|---|---|
| Atomic migration core | Software Architect + Backend Architect | `vmware_to_proxmox/engine.py`, `webui/dashboard/services.py`, `webui/dashboard/models.py`, `webui/dashboard/management/commands/worker.py` | Introduce a durable migration ledger and a job-scoped execution context; claim jobs with row locking plus a claim token; persist checkpoints for VM create/import/attach so a crash can be reconciled deterministically; guarantee that every in-flight job is either fully committed or fully cleaned up on restart. |
| SSH/SFTP transport | Infrastructure Maintainer + Security Engineer | `vmware_to_proxmox/proxmox.py` | Add a keyed connection pool for SSH and SFTP sessions; keep `_run(args)` list-based and command-safe; validate VM names, storage IDs, bridges, and VLAN tags; centralize session lifecycle cleanup so jobs do not leak connections into the next run. |
| SDN/VLAN and multi-NIC mapping | Solutions Architect | `vmware_to_proxmox/proxmox.py`, `vmware_to_proxmox/engine.py`, `webui/dashboard/models.py`, `webui/dashboard/forms.py` | Expand network discovery to surface standard bridges, VLAN-aware bridges, and SDN VNets; change NIC mapping from a single string bridge to an ordered per-NIC structure keyed by NIC index, label, and MAC; pass VLAN tags through to `qm set --netN ... ,tag=<vlan>` when the target bridge is VLAN-aware. |
| No-refresh migration UX | Frontend Developer | `templates/dashboard/wizard.html`, `webui/dashboard/views.py`, `templates/dashboard/job_detail.html` | Replace the full-page submit with an async launch flow that receives `job_id`, disables the launch button, and polls `job_status_api` until completion; reuse the existing job-detail polling patterns for progress bars and logs; keep VMX auto-fill from overwriting user-entered disk/NIC overrides. |

#### Atomicity target state

- **Plan first, commit last** — every migration step writes a checkpoint before making a Proxmox mutation.
- **Per-job ledger** — record the VMID, imported volume IDs, and temp paths under a unique job token so crash recovery can find them.
- **Reconciliation on startup** — if a job never reaches the final commit marker, scan Proxmox for its artifacts and destroy or detach them before the job is retried.
- **Job-scoped clients** — never reuse a mutated `MigrationEngine` or `ProxmoxClient` between jobs without a full `reset()`/`close()` cycle.
- **Practical guarantee** — the workflow should be designed so the only terminal states are `SUCCEEDED` or `FAILED_WITH_CLEANUP`; anything else is treated as an incomplete transaction and is reconciled before the next job starts.

---

## 9. How a Migration Flows (End-to-End)

```
User fills wizard (wizard.html)
  ↓ Step 1: select ProxmoxHost + optional VMwareHost
  ↓ Step 2: browse HOST filesystem (SFTP modal) → scan VMDKs
  ↓ Step 3: discover live storages/bridges → set per-disk / per-NIC overrides
  ↓ async launch submit
  MigrationJob created in DB (status=PENDING, proxmox_host_id, vmware_host_id set)
  ↓
Worker polls DB → finds PENDING job (the HTTP run endpoint uses row locking, but the worker path should be hardened similarly for multi-worker deployments)
  ↓ execute_job(job)  [services.py]
  ↓ compatibility report / dry-run gate
  │   - validate snapshots, vTPM, passthrough, and disk-shrink policy
  │   - resolve storage, bridge, fallback bridge, and (future) cluster-aware placement
  │   - natural progress boundaries for future WebSocket/SSE telemetry
  ↓ _engine_from_job(job)  → builds MigrationEngine from registered host or config file
  ↓ engine.migrate_local_disks_or_archive() OR engine.migrate_vm()
  │
  ├── VMware mode:
  │     VMwareClient.download_vm_disks() → disks land in local tmpdir
  │
  └── Local mode:
        archive? → extract on HOST via SSH → list VMDKs inside
        plain VMDK → use host-absolute path directly
  ↓
  ProxmoxClient.create_vm()      → SSH: qm create <vmid> ...
  ProxmoxClient.import_disk()    → SSH: qm importdisk <vmid> <HOST_PATH> <storage>
  ProxmoxClient.attach_disk()    → SSH: qm set <vmid> --scsiN volume_id
  ProxmoxClient.add_network()    → SSH: qm set <vmid> --netN virtio,bridge=vmbr0[,tag=<vlan>]
  GuestRemediator.write_script() → generates remediation.sh for the guest
  ProxmoxClient.start_vm()       → SSH: qm start <vmid>
  ↓
  If rollback_on_failure is enabled, the engine attempts `qm destroy <vmid>` on failure
  ↓
  job.status = SUCCEEDED / FAILED
  job.result / job.error / job.logs written to DB
  ↓
Wizard UI polls `job_status_api` for live status/logs and updates progress without a full page refresh
```

---

## 10. Where to Start for Common Tasks

| Task | File(s) to edit |
|---|---|
| Add a registered host field | `webui/dashboard/models.py` + new migration + `forms.py` + `hosts.html` |
| Add a wizard form field | `webui/dashboard/forms.py`, `webui/dashboard/models.py`, new migration, `wizard.html` |
| Change how Proxmox commands run | `vmware_to_proxmox/proxmox.py` |
| Change the migration workflow steps | `vmware_to_proxmox/engine.py` |
| Add a new API endpoint | `webui/dashboard/views.py` + `webui/dashboard/urls.py` |
| Change the wizard UI / JS | `templates/dashboard/wizard.html` |
| Change styling | `static/dashboard.css` |
| Add a new config option | `vmware_to_proxmox/config.py` (dataclass + loader) + `config.example.yaml` |
| Change Docker setup | `docker-compose.yml`, `Dockerfile`, `entrypoint.sh` |
| Change network/datastore mapping logic | `vmware_to_proxmox/config.py` (`bridge_for_network`, `storage_for_datastore`) |
