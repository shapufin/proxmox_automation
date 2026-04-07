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
| **Local Disk** | You pre-copy VMDK files to a staging directory on the Proxmox host; the app imports them via `qm importdisk` over SSH |

---

## 2. Repository Layout

```
vmware_to_proxmox/           ← Python library (core logic)
│   config.py                  AppConfig dataclass loader (YAML → Python)
│   models.py                  Shared dataclasses: DiskFormat, ProxmoxStorageSpec, ProxmoxBridgeSpec, etc.
│   engine.py                  MigrationEngine — orchestrates the full migration workflow
│   proxmox.py                 ProxmoxClient — all Proxmox interaction (SSH CLI + REST API + SFTP)
│   vmware.py                  VMwareClient — pyvmomi connection, VM listing, disk export
│   disk.py                    Disk format helpers (qcow2, raw, vmdk detection)
│   guest.py                   Guest remediation (fstab rewrite, qemu-agent install)
│   cli.py                     Click CLI entrypoint (non-web usage)
│   logger.py                  Logging setup

webui/                       ← Django project
│   settings.py                Django settings (SQLite, static files, env vars)
│   urls.py                    Root URL router (includes dashboard app)
│   dashboard/
│       models.py              MigrationJob Django model
│       views.py               All HTTP views + JSON API endpoints
│       forms.py               MigrationJobForm, ConfigProfileForm, DiskBrowseForm
│       services.py            Business logic between views and engine
│       urls.py                URL patterns for the dashboard app
│       migrations/            Django DB migrations

templates/dashboard/         ← Jinja2/Django HTML templates
│   base.html                  Nav bar, CSS includes
│   index.html                 Main dashboard (wizard form, job table, JS state machine)
│   job_detail.html            Single job log view
│   config_editor.html         YAML config profile editor
│   browse.html                Legacy disk browser (mostly replaced by modal)

static/
│   dashboard.css              All CSS (dark theme, wizard steps, cards, badges)

config.example.yaml          ← Copy to config.yaml and fill in credentials
docker-compose.yml           ← Defines migrate / web / worker services
Dockerfile                   ← Single image used by all three services
requirements.txt             ← Python dependencies
pyproject.toml               ← Build config (setuptools package discovery)
```

---

## 3. Key Python Files — What Each Does

### `vmware_to_proxmox/config.py`
- Defines `AppConfig`, `VmwareConfig`, `ProxmoxConfig`, `MigrationConfig`, `SshConfig` dataclasses.
- `AppConfig.load(path)` reads `config.yaml` (YAML → dataclasses).
- Has `_coerce_int` / `_coerce_bool` helpers so blank YAML fields don't crash.
- **Important fields in `ProxmoxConfig`:**
  - `ssh_enabled`, `ssh_host`, `ssh_port`, `ssh_username`, `ssh_password`, `ssh_private_key` — SSH to Proxmox host
  - `api_host`, `api_user`, `api_token_name`, `api_token_value`, `api_verify_ssl` — Proxmox REST API (port 8006)

### `vmware_to_proxmox/proxmox.py`
The most important file. `ProxmoxClient` handles:

| Method | What it does |
|---|---|
| `_api_client()` | Builds a `proxmoxer.ProxmoxAPI` instance; tests with `api.version.get()`; on DNS/connection failure sets a sentinel and never retries |
| `_run(args)` | Runs a CLI command locally OR via SSH depending on `ssh_enabled` |
| `_run_remote(args)` | SSH command execution via paramiko |
| `list_storages()` | Uses REST API first, falls back to `pvesm status` via SSH |
| `list_bridges()` | Uses REST API first, falls back to `pvesh get /nodes/{node}/network` via SSH |
| `list_remote_dir(path)` | **SFTP** directory listing on the Proxmox HOST (not the LXC). Used by the file browser. |
| `read_remote_file(path)` | Reads a file from the Proxmox HOST via SFTP (used to read manifest.json) |
| `create_vm()` | `qm create` via SSH |
| `import_disk()` | `qm importdisk` via SSH — **path must be a host-absolute path** |
| `ensure_prerequisites()` | Skips local binary check when SSH or API is configured |

**Critical architecture note:** The app runs inside an LXC. `qm`, `pvesh`, `pvesm` do **not** exist inside the LXC. All CLI calls go over SSH to the host. The host-side absolute path (e.g. `/var/lib/vz/dump/myvm/disk.vmdk`) must be used in `qm importdisk`, not the LXC-local path.

### `vmware_to_proxmox/engine.py`
`MigrationEngine` — the main workflow class:
- `inventory()` → returns `{vmware_vms, proxmox_storages, proxmox_bridges}` for the GUI
- `run_job(job)` → full migration: plan → create VM on Proxmox → import disks → attach network → remediate guest
- Instantiates both `VMwareClient` and `ProxmoxClient` from config

### `webui/dashboard/services.py`
Bridge between views and engine:
- `get_engine()` → loads config and returns a `MigrationEngine` instance
- `resolve_stage_path(path)` → accepts **any absolute path** (not locked to `MIGRATION_STAGE_ROOT`)
- `list_stage_entries(path)` → returns `{directory, folders, files}` for a given path

### `webui/dashboard/views.py`
All HTTP handlers:

| View | URL | Purpose |
|---|---|---|
| `dashboard` | `GET /` | Main page — loads inventory, renders wizard form |
| `launch_job` | `POST /launch/` | Creates a `MigrationJob` DB record |
| `run_pending_job` | `POST /jobs/<id>/run/` | Executes a pending job synchronously |
| `job_detail` | `GET /jobs/<id>/` | Job log page |
| `config_profile_editor` | `GET /configs/` | YAML config editor |
| `save_profile` | `POST /configs/save/` | Saves a named config profile |
| `proxmox_status` | `GET /api/proxmox-status/` | **JSON** — live storages + bridges from Proxmox host |
| `browse_directory` | `GET /api/browse-directory/?path=` | **JSON** — HOST filesystem listing via SFTP |
| `vmdk_scan` | `GET /api/vmdk-scan/?path=` | **JSON** — VMDKs in a host dir + auto-detect manifest.json |

### `webui/dashboard/forms.py`
- `MigrationJobForm` — wizard form with dynamic choices for storage, bridge, VM name, source files
- `set_storage_choices(choices)` / `set_bridge_choices(choices)` — called from views with live Proxmox data

### `webui/dashboard/models.py`
- `MigrationJob` — Django model: name, mode, config_profile, vm_name, manifest_path, source_paths, storage, bridge, disk_format, dry_run, status, logs, timestamps

---

## 4. Frontend (`templates/dashboard/index.html`)

Implements a **3-state wizard** entirely in vanilla JS (no framework):

| State | UI element | API called | What happens |
|---|---|---|---|
| **A — VMDK Selection** | "Browse Host" button → modal | `GET /api/browse-directory/` | Paramiko SFTP lists HOST filesystem in a modal overlay; `.vmdk` files highlighted blue |
| **A continued** | "Scan for VMDKs" button | `GET /api/vmdk-scan/` | Finds all `.vmdk` in the host dir, builds checkboxes with host-absolute paths, auto-fills manifest field |
| **B — Storage** | "Discover Storages & Networks" | `GET /api/proxmox-status/` | Populates storage `<select>` with live Proxmox data |
| **C — Network** | Same button | same response | Populates bridge `<select>` |

**Top panel:** "Test & Refresh" button also calls `/api/proxmox-status/` and updates the status cards.

---

## 5. Docker / Deployment

```yaml
# docker-compose.yml — three services, one image
migrate:   runs Django migrations once (service_completed_successfully)
web:       gunicorn, port 8000
worker:    python manage.py worker (polls DB for pending jobs)
```

**Environment variables:**
| Variable | Default | Purpose |
|---|---|---|
| `VMWARE_TO_PROXMOX_CONFIG` | `/config/config.yaml` | Path to config.yaml inside container |
| `VMWARE_TO_PROXMOX_CONFIG_DIR` | `/app/configs` | Directory for named config profiles |
| `VMWARE_TO_PROXMOX_STAGE_ROOT` | `/data/staging` | Fallback staging root for relative paths |
| `DJANGO_SECRET_KEY` | `change-me` | **Must be changed in production** |
| `DJANGO_ALLOWED_HOSTS` | `*` | Restrict in production |

**Volume mounts (all services):**
```
./config.yaml   → /config/config.yaml  (read-only)
./configs/      → /app/configs
./data/         → /app/data
./staging/      → /data/staging
# Optional bind-mount for host VMDKs (uncomment in docker-compose.yml):
# /var/lib/vz/dump → /mnt/vmware_staging  (read-only)
```

---

## 6. Configuration (`config.yaml`)

Copy `config.example.yaml` → `config.yaml`. Key sections:

```yaml
proxmox:
  node: pve               # Proxmox node name
  api_host: 18.0.0.1     # HOST IP — not the LXC IP
  api_user: root@pam
  api_token_name: migration
  api_token_value: "UUID-HERE"   # pveum user token add root@pam migration --privsep=0
  api_verify_ssl: false

  ssh_enabled: true
  ssh_host: 18.0.0.1     # same as api_host
  ssh_username: root
  ssh_password: ""        # or use ssh_private_key

migration:
  dry_run: true           # always start with true
```

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

1. **LXC isolation** — `qm`, `pvesh`, `pvesm` are NOT available inside the LXC/container. All CLI commands execute on the Proxmox host over SSH via paramiko.
2. **Host-absolute paths** — `qm importdisk <vmid> <path> <storage>` receives the path as seen by the HOST, not the LXC. The SFTP browser and VMDK scanner always return host-side paths.
3. **File visibility** — two options:
   - SSH/SFTP (configured, active by default when `ssh_enabled: true`)
   - Bind-mount: uncomment `# - /var/lib/vz/dump:/mnt/vmware_staging:ro` in `docker-compose.yml`
4. **SDN discovery** — bridges come from `GET /nodes/{node}/network` via the REST API or `pvesh` over SSH. `/etc/pve/sdn` is not parsed directly but could be bind-mounted (`/etc/pve:/etc/pve:ro`) into the LXC for direct access.
5. **SQLite** — used for the job database. Path: `./data/db.sqlite3`. Not suitable for multi-host deployments.

---

## 9. How a Migration Flows (End-to-End)

```
User fills wizard (index.html)
  ↓ POST /launch/
  MigrationJob created in DB (status=PENDING)
  ↓
Worker polls DB → finds PENDING job
  ↓ execute_job(job)  [services.py]
  ↓ engine.run_job(job)  [engine.py]
  ├── VMware mode: VMwareClient.export_vm() → download VMDK to staging
  └── Local mode:  use host-absolute path from job.source_paths
  ↓
  ProxmoxClient.create_vm()      → SSH: qm create <vmid> ...
  ProxmoxClient.import_disk()    → SSH: qm importdisk <vmid> <HOST_PATH> <storage>
  ProxmoxClient.attach_disk()    → SSH: qm set <vmid> --scsi0 ...
  ProxmoxClient.add_network()    → SSH: qm set <vmid> --net0 virtio,bridge=vmbr0
  GuestRemediation.run()         → SSH into new VM: fix fstab, install qemu-agent
  ↓
  job.status = SUCCEEDED / FAILED
  Logs written to job.log in DB
```

---

## 10. Where to Start for Common Tasks

| Task | File(s) to edit |
|---|---|
| Add a new migration option / form field | `webui/dashboard/forms.py`, `webui/dashboard/models.py`, new migration file |
| Change how Proxmox commands run | `vmware_to_proxmox/proxmox.py` |
| Change the migration workflow steps | `vmware_to_proxmox/engine.py` |
| Add a new API endpoint | `webui/dashboard/views.py` + `webui/dashboard/urls.py` |
| Change the wizard UI / JS state machine | `templates/dashboard/index.html` |
| Change styling | `static/dashboard.css` |
| Add a new config option | `vmware_to_proxmox/config.py` (dataclass + loader) + `config.example.yaml` |
| Change Docker setup | `docker-compose.yml`, `Dockerfile` |
