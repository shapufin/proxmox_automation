# VMware to Proxmox Migration Tool

Production-focused migration utility for moving **Linux-only VMware ESXi virtual machines** into **Proxmox VE**.

The primary operator experience is now a **Dockerized Django GUI** that can run on a management host and talk to Proxmox over SSH. The CLI still exists as a fallback for direct execution.

## What it does

- Discovers VMware VMs and Proxmox node resources dynamically.
- Migrates VMDK disks into **qcow2** or **raw**.
- Creates Proxmox VMs with correct CPU, memory, firmware, storage, and network settings.
- Handles **BIOS or UEFI/OVMF** provisioning.
- Generates and optionally executes guest remediation for:
  - `qemu-guest-agent`
  - `open-vm-tools` cleanup
  - `initramfs` regeneration with VirtIO drivers
  - GRUB refresh
  - `/etc/fstab` UUID normalization
- Supports **single** VM or **batch** migration.

**Detailed migration flow**: See [MIGRATION_FLOW.md](MIGRATION_FLOW.md) for step-by-step logic from VMware disk export to Proxmox guest remediation, including stage-based checkpointing, rollback procedures, and error handling strategies.

## Important limitations

This tool is designed for **Linux guests only**.

It intentionally rejects or warns on VMware features that usually block a clean migration:

- Snapshots still present
- vTPM / encrypted VMs
- RDM / pass-through disks
- PCI passthrough devices
- Suspended state
- Unsupported virtual hardware edge cases

## Host requirements

For the Dockerized GUI, run the container on a management host with SSH access to Proxmox and access to the staged disk files.

If you choose the direct CLI fallback, run this on the **Proxmox host** as `root`.

Required packages on the Proxmox node:

- `qm`
- `pvesh`
- `pvesm`
- `qemu-img`
- `python3`
- `python3-venv`
- `python3-pip`
- `ssh` access to ESXi

For the Dockerized GUI deployment, also install:

- `docker`
- `docker compose`

## Python dependencies

Install Python dependencies with:

```bash
pip3 install -r requirements.txt
```

If you want an installed command on the Proxmox host, install the project itself:

```bash
pip3 install .
```

For the easiest Proxmox-hosted experience, use the bundled launcher:

```bash
bash pve-migrate.sh inventory --config config.yaml
```

## Dockerized GUI

Start the GUI and worker with:

```bash
docker compose up --build
```

Then open:

```text
http://localhost:8000
```

The stack runs three services from the same image:

- **migrate** — runs `manage.py migrate` and `collectstatic` once then exits. `web` and `worker` wait for it to complete successfully before starting.
- **web** — Gunicorn serving the Django UI on port 8000.
- **worker** — polls the DB for pending `MigrationJob` records and executes them.

All three services share a single SQLite database at `/app/data/db.sqlite3` via the `./data` volume mount. The `DJANGO_DB_PATH` env var pins the path so every container uses the same file.

The GUI lets you:

- Register Proxmox and VMware hosts (no config file needed)
- Browse the staged local disks on the Proxmox host via SSH/SFTP
- Select one or more disks or archives for a migration job
- Set per-disk storage and per-NIC bridge overrides
- Launch and monitor migration jobs

## Usage

Create a config file from the sample:

```bash
cp config.example.yaml config.yaml
```

List resources:

```bash
bash pve-migrate.sh inventory --config config.yaml
```

Preview a migration plan:

```bash
bash pve-migrate.sh plan --config config.yaml --vm MyLinuxVM
```

Dry-run a migration:

```bash
bash pve-migrate.sh migrate --config config.yaml --vm MyLinuxVM --dry-run
```

Migrate multiple VMs:

```bash
bash pve-migrate.sh migrate --config config.yaml --vm vm1 --vm vm2 --vm vm3
```

### Local disk import mode

If you already copied the VM disks to the Proxmox host, use the local mode with the exported manifest:

```bash
bash pve-migrate.sh migrate --config config.yaml --source-mode local --manifest /path/to/vm.manifest.json --disk-path /path/to/disk.vmdk --disk-path /path/to/otherdisk.vmdk
```

You can also point at a directory of disks:

```bash
bash pve-migrate.sh migrate --config config.yaml --source-mode local --manifest /path/to/vm.manifest.json --disk-dir /path/to/disks
```

You can still run the package directly if you prefer:

```bash
python3 -m vmware_to_proxmox plan --config config.yaml --vm MyLinuxVM
```

## Notes on EFI and drivers

For UEFI-based guests, the tool creates a Proxmox EFI disk and sets OVMF/OVMF vars.

For Linux guests, the remediation step updates the guest so it boots cleanly with VirtIO devices.

## Production recommendation

Test on a non-critical VM first, then run batch migrations after the resulting Proxmox VM boots successfully and validates networking and storage.

## Deployment runbook

### 1. Prepare the workspace

```bash
cp config.example.yaml config.yaml
mkdir -p configs data staging
```

### 2. Edit your active config

- Update `config.yaml` for the default deployment profile.
- Or use the GUI **Configs** page to create additional YAML profiles in `configs/`.

### 3. Stage local disks

- Copy VM disks from VMware into the `staging/` directory.
- Keep each VM in its own folder when possible.
- Put the exported manifest JSON in the same folder or somewhere inside the staged path.

### 4. Start the stack

```bash
docker compose up --build
```

The `migrate` service runs database migrations automatically before `web` and `worker` start. No manual `manage.py migrate` step is needed.

### 5. Open the GUI

```text
http://localhost:8000
```

### 6. Register hosts

- Go to **Hosts** in the sidebar.
- Add a Proxmox host (API token + SSH credentials).
- Optionally add a VMware host for direct VM migration.
- Use **Test Connection** to verify credentials before launching a job.

### 7. Run a safe first test

- Leave **Dry run** enabled.
- In the wizard, select a registered Proxmox host.
- Select a VMware VM or a staged local-disk folder.
- Review storage, bridge, and disk format.
- Run the job only when the plan looks correct.

## Production checklist

- **Proxmox access**
  - Confirm the container host can reach the Proxmox node over SSH.
  - Confirm the Proxmox account can run `qm`, `pvesh`, and `pvesm`.

- **VMware access**
  - Confirm the VMware credentials can list and read the source VM.
  - Confirm the source VM is powered off before a real migration.

- **Disk staging**
  - Confirm the staged disk files are present.
  - Confirm the manifest matches the staged disks.
  - Confirm multi-disk VMs have all disks copied.

- **Safety defaults**
  - Keep dry-run enabled for the first execution.
  - Validate the plan before disabling dry-run.

- **Container runtime**
  - Confirm `docker compose up --build` starts all three services cleanly.
  - Confirm the `migrate` service exits 0 before `web` and `worker` start.
  - Confirm the web UI loads at port `8000`.
  - Confirm the `configs/`, `data/`, and `staging/` volumes persist.
  - Confirm `DJANGO_SECRET_KEY` is set to a random value (not `change-me`).

## Live validation procedure

Use this sequence on the first real VM:

1. **Inventory**
   - Open the GUI and verify VMware VMs and Proxmox resources are listed.

2. **Plan**
   - Select the VM or local folder.
   - Check the config profile.
   - Review storage, bridge, and disk format.

3. **Dry run**
   - Run the job with dry-run still enabled.
   - Confirm the backend returns a valid result with no unexpected errors.

4. **Real migration**
   - Disable dry-run for a single non-critical VM.
   - Run the migration.
   - Verify the VM starts and boots on Proxmox.

5. **Post-check**
   - Verify network connectivity.
   - Verify disk attachments.
   - Verify guest agent/remediation behavior if enabled.

6. **Batch rollout**
   - Only after the first VM passes validation, proceed with additional VMs.
