# VMware to Proxmox Migration Flow

This document details the step-by-step logic of the migration process from VMware disk export to Proxmox guest remediation.

## Overview

The migration process follows a **stage-based checkpointed workflow** with resumability via a migration ledger. Each stage tracks its status (pending/running/succeeded/failed), timestamps, and artifacts for recovery and rollback.

## Architecture Components

- **MigrationEngine** (`vmware_to_proxmox/engine.py`): Core orchestrator
- **VmwareClient** (`vmware_to_proxmox/vmware.py`): VMware vSphere API and SFTP client
- **ProxmoxClient** (`vmware_to_proxmox/proxmox.py`): Proxmox API and SSH CLI client
- **MigrationJob** (`webui/dashboard/models.py`): Django model for job persistence
- **Migration Ledger**: JSONField tracking stage progress and artifacts

## Migration Modes

### 1. VMware Direct Mode
- Connects to VMware vSphere API
- Discovers VMs via pyVmomi
- Downloads VMDK files via SFTP from ESXi host
- Migrates to Proxmox with full hardware fidelity

### 2. Local Disk Mode
- Uses pre-exported disk files (VMDK, archives)
- Reads VMX manifest for hardware specification
- Imports local disks into Proxmox storage
- Useful for air-gapped or pre-staged migrations

## Stage-Based Migration Flow

### Stage 1: VM Creation (`vm_created`)

**Purpose**: Create the Proxmox VM skeleton with CPU, memory, and firmware configuration.

**Steps**:
1. **VMware Discovery** (VMware mode only)
   - Connect to vSphere API via `VmwareClient.connect()`
   - Retrieve VM by name: `VmwareClient.get_vm_by_name()`
   - Parse hardware spec: `_vm_to_spec()` extracts CPU, memory, disks, NICs
   - Validate compatibility: check for Linux guest, powered off state, no snapshots

2. **Hardware Mapping**
   - Map guest OS to Proxmox ostype: `_map_guest_os_to_ostype()`
   - Map SCSI controller type: `_map_scsi_to_proxmox()`
   - Map NIC model: `_map_nic_model()`
   - Resolve firmware mode (BIOS/UEFI): `_resolve_firmware()`

3. **Proxmox VM Creation**
   - Get next VMID: `ProxmoxClient.next_vmid()`
   - Resolve target storage: `_resolve_storage()` with free-space selection
   - Resolve target bridge: `_resolve_bridge()` with network mapping
   - Create VM: `ProxmoxClient.create_vm()` with:
     - CPU cores, sockets, memory
     - ostype, machine type (q35)
     - SCSI controller (virtio-scsi-single)
     - BIOS/UEFI configuration
     - QEMU guest agent enabled

**Artifacts Tracked**:
- `vmid`: Proxmox VM ID
- `proxmox_name`: Sanitized VM name

**Failure Handling**:
- Rollback: `ProxmoxClient.destroy_vm()` if `rollback_on_failure=true`
- Ledger reset for retry after successful cleanup

---

### Stage 2: Disk Export (`disks_exported`)

**Purpose**: Export and stage VMware disks for conversion (VMware mode only).

**Steps**:
1. **Disk Discovery**
   - Extract disk paths from VM spec: `VmwareClient.disk_source_paths()`
   - Resolve ESXi paths: `remote_esxi_path()` converts `[datastore] path` to `/vmfs/volumes/datastore/path`

2. **SFTP Download**
   - Establish SSH connection to ESXi host
   - Download each VMDK: `VmwareClient.download_file()`
   - Store in staging directory: `stage_root()/jobs/job-{id}/`

3. **Archive Handling** (if applicable)
   - Detect archive type: `detect_archive_type()` (.zip, .7z, .tar.gz, etc.)
   - Extract on Proxmox host: `ProxmoxClient.extract_archive()`
   - Preview archive contents: `ProxmoxClient.peek_archive()`

**Artifacts Tracked**:
- `source_paths`: Original VMware disk paths
- `export_paths`: Local staged disk paths
- `working_dir`: Staging directory for the job

**Failure Handling**:
- Partial downloads are cleaned up on next run
- Ledger preserves successful exports for resume

---

### Stage 3: Disk Import (`disks_imported`)

**Purpose**: Convert disks to target format and import into Proxmox storage.

**Steps**:
1. **Disk Conversion**
   - Detect source format: `detect_disk_format()` via qemu-img
   - Convert to target format: `convert_disk()` (VMDK → qcow2/raw)
   - Apply resize if specified: `resize_disk()` with shrink protection
   - Validate disk integrity: `sha256_file()` for checksums

2. **Proxmox Import**
   - Import disk: `ProxmoxClient.import_disk()` via `qm importdisk`
   - Parse volume ID from output (regex fallback for localization)
   - Track import record with attachment status

3. **Disk Attachment**
   - Attach to VM: `ProxmoxClient.attach_disk()` via `qm set`
   - Map to SCSI slot: `scsi0`, `scsi1`, etc.
   - Set cache mode: writeback for performance
   - Mark volume as attached in ledger

**Artifacts Tracked**:
- `volume_ids`: List of imported Proxmox volume IDs
- `imported_disks`: Array of import records:
  ```json
  {
    "source": "original_path",
    "local_path": "staged_path",
    "converted_path": "converted_path",
    "volume_id": "local-lvm:vm-100-disk-0",
    "slot": "scsi0",
    "attached": true,
    "target_storage": "local-lvm",
    "source_datastore": "datastore1"
  }
  ```

**Failure Handling**:
- Failed imports are retried from conversion step
- Attached volumes are tracked to prevent double-attachment on resume
- Rollback removes imported volumes: `ProxmoxClient.remove_volume()`

---

### Stage 4: NIC Configuration (`nics_configured`)

**Purpose**: Configure network interfaces with bridge mapping and VLAN tags.

**Steps**:
1. **NIC Discovery**
   - Extract NIC specs from VM or VMX manifest
   - Map VMware network to Proxmox bridge: `bridge_for_network()`
   - Apply per-NIC overrides from `nic_bridge_map`

2. **NIC Creation**
   - Add network device: `ProxmoxClient.add_network()`
   - Set parameters:
     - Model: virtio (default), vmxnet3, e1000
     - Bridge: target bridge (vmbr0, etc.)
     - MAC address: preserve if `preserve_mac=true`
     - VLAN tag: `tag=<vlan>` for SDN/VLAN-aware networks

3. **Fallback Handling**
   - If no NICs discovered, use `fallback_nic_bridge`
   - Create single NIC with default settings

**Artifacts Tracked**:
- `networks`: Array of NIC records:
  ```json
  {
    "index": 0,
    "label": "Network adapter 1",
    "bridge": "vmbr0",
    "macaddr": "00:50:56:xx:xx:xx",
    "model": "virtio",
    "vlan": 100
  }
  ```

**Failure Handling**:
- Invalid bridges are validated before VM creation
- Missing fallback bridge raises configuration error

---

### Stage 5: Guest Remediation (`remediation_applied`)

**Purpose**: Apply guest OS adjustments for VirtIO drivers and boot configuration.

**Steps**:
1. **Remediation Script Generation**
   - Detect guest OS from VM spec
   - Generate script for:
     - Installing qemu-guest-agent
     - Removing open-vm-tools
     - Regenerating initramfs with VirtIO drivers
     - Updating GRUB configuration
     - Normalizing `/etc/fstab` with UUIDs

2. **Script Execution** (if enabled)
   - Mount disk image (qemu-nbd or guestfish)
   - Apply remediation script to guest filesystem
   - Unmount disk image

3. **Boot Configuration**
   - Set boot order: `ProxmoxClient.set_boot_order()`
   - Configure EFI disk if UEFI: `ProxmoxClient.add_efi_disk()`

**Artifacts Tracked**:
- `script_path`: Path to generated remediation script
- `applied`: Boolean indicating if remediation was executed

**Failure Handling**:
- Remediation failures are non-fatal (VM may boot with manual fixes)
- Script path preserved for manual re-execution

---

### Stage 6: VM Start (Optional)

**Purpose**: Boot the migrated VM for validation (if `start_after_import=true`).

**Steps**:
1. **VM Start**
   - Start VM: `ProxmoxClient.start_vm()`
   - Wait for QEMU guest agent to respond
   - Verify network connectivity

**Failure Handling**:
- Start failures are logged but don't fail the job
- Manual start required if automatic start fails

---

## Cleanup and Rollback

### Cleanup State Tracking

The ledger maintains a `cleanup` section:
```json
{
  "status": "pending",
  "started_at": null,
  "completed_at": null,
  "deleted_volume_ids": [],
  "deleted_vmid": null,
  "errors": []
}
```

### Rollback Procedure

On fatal failure with `rollback_on_failure=true`:

1. **Destroy VM** (if created)
   - `ProxmoxClient.destroy_vm(vmid)` with `--purge`
   - Track deleted VMID in cleanup state

2. **Remove Volumes** (if imported)
   - Iterate through `imported_disks` volume IDs
   - `ProxmoxClient.remove_volume(volume_id)`
   - Track deleted volumes in cleanup state

3. **Reset Ledger** (if cleanup succeeds)
   - Reset all stages to `pending`
   - Clear artifacts
   - Allow fresh retry attempt

4. **Preserve Failed State** (if cleanup fails)
   - Keep failed stage status
   - Log cleanup errors
   - Require manual intervention

---

## Ledger Reconciliation

The `reconcile()` method handles ledger evolution:

1. **Schema Versioning**
   - Check `schema_version` field
   - Apply migrations if version mismatch

2. **Stage State Preservation**
   - Merge incoming ledger with default template
   - Preserve completed stages for resume
   - Reset failed stages for retry

3. **Artifact Validation**
   - Validate artifact structure
   - Sanitize malformed data
   - Ensure type consistency

---

## Error Handling Strategy

### Transient Errors
- Network timeouts: retry with exponential backoff
- SSH connection failures: reconnect and retry
- Proxmox API rate limits: wait and retry

### Permanent Errors
- Invalid VM configuration: fail fast with clear error
- Unsupported hardware: block with recommendation
- Disk conversion failure: preserve source for manual intervention

### Resume Logic
- Check stage status in ledger
- Skip succeeded stages
- Retry from last failed stage
- Preserve successful artifacts

---

## Performance Considerations

### Parallel Operations
- Disk exports: sequential (SFTP bandwidth limit)
- Disk conversions: sequential (qemu-img CPU intensive)
- Disk imports: sequential (Proxmox storage I/O)
- NIC configuration: batched (single `qm set` call)

### Resource Optimization
- Staging directory: per-job isolation prevents conflicts
- Archive extraction: on-host extraction avoids network transfer
- Disk conversion: target format selection (qcow2 for space, raw for performance)

### Monitoring Points
- Stage duration timestamps
- Disk conversion progress (qemu-img -p flag)
- Ledger persistence after each stage
- Cleanup success/failure tracking

---

## Security Considerations

### Credential Handling
- VMware credentials: stored in VMwareHost model (encrypted at rest recommended)
- Proxmox credentials: stored in ProxmoxHost model (API tokens preferred)
- SSH keys: stored in database (consider external secret management)

### Path Validation
- User-supplied paths: validated against staging directory
- SFTP operations: restricted to allowed prefixes
- Command injection: shlex.quote() for all subprocess calls

### SSH Security
- Known hosts: AutoAddPolicy (security risk, should use known_hosts)
- Connection pooling: not implemented (opportunity for improvement)
- Key-based auth: supported but password auth fallback exists

---

## Future Enhancements

### Task Graph (DAG) Model
Replace linear stages with dependency graph for parallel execution:
- Independent disk conversions can run in parallel
- NIC configuration can proceed while disks import
- Remediation can start after first disk attachment

### Progress Telemetry
- WebSocket/SSE for real-time progress updates
- Per-disk conversion percentage
- Stage-level ETA calculation

### Enterprise Features
- HA-group aware VM placement
- SDN/VNet-aware scheduling
- Storage performance-class policies
- ZFS/Ceph tuning parameters

### Validation Improvements
- Pre-migration dry-run with full validation
- Post-migration health checks
- Automated rollback testing
