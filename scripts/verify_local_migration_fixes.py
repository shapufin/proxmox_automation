#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

from vmware_to_proxmox.config import AppConfig, MigrationConfig, ProxmoxConfig, VmwareConfig
from vmware_to_proxmox.engine import MigrationEngine
from vmware_to_proxmox.models import ProxmoxStorageSpec
from webui.dashboard.forms import MigrationJobForm


def _build_form(source_paths: list[Path]) -> MigrationJobForm:
    form = MigrationJobForm(
        data={
            "name": "local-migration-verification",
            "mode": "local",
            "vm_name": "",
            "source_paths": [str(path) for path in source_paths],
            "storage": "local-zfs",
            "bridge": "vmbr0",
            "dry_run": True,
            "start_after_import": False,
        }
    )
    form.set_source_choices([(str(path), path.name) for path in source_paths])
    form.set_config_profile_choices([])
    form.set_vm_choices([])
    form.set_storage_choices([])
    form.set_bridge_choices([])
    return form


def _build_engine() -> MigrationEngine:
    config = AppConfig(
        vmware=VmwareConfig(host="", username="", password="", port=443, ssh_enabled=False),
        proxmox=ProxmoxConfig(node="pve", default_storage="local-zfs", default_bridge="vmbr0"),
        migration=MigrationConfig(dry_run=True, guest_remediation=False, rollback_on_failure=False),
    )
    engine = MigrationEngine(config, logger=logging.getLogger("verify-local-migration-fixes"))
    engine.proxmox.ensure_prerequisites = lambda: None
    engine.proxmox.list_storages = lambda: [
        ProxmoxStorageSpec(
            storage="local-zfs",
            content="images",
            storage_type="dir",
            total=0,
            used=0,
            available=0,
            shared=False,
            active=True,
        )
    ]
    return engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify local migration fixes in dry-run mode.")
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary test directory instead of deleting it.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    temp_dir = tempfile.TemporaryDirectory() if not args.keep_temp else None
    try:
        root = Path(temp_dir.name if temp_dir is not None else tempfile.mkdtemp(prefix="verify-local-migration-"))
        disk1 = root / "Ubuntu_20.04.4_VM_LinuxVMImages.COM.vmdk"
        disk2 = root / "Ubuntu_20.04.4_VM_LinuxVMImages.COM-000001.vmdk"
        disk1.write_text("disk-1", encoding="utf-8")
        disk2.write_text("disk-2", encoding="utf-8")

        form = _build_form([disk1, disk2])
        if not form.is_valid():
            print("FORM_VALID=false")
            print(json.dumps(form.errors, indent=2, sort_keys=True))
            return 1

        engine = _build_engine()
        result = engine.migrate_local_disks(
            vm_name="",
            manifest_path=None,
            disk_paths=[disk1, disk2],
            storage="local-zfs",
            bridge="vmbr0",
            dry_run=True,
            start_after_import=False,
        )

        payload = {
            "form_valid": True,
            "autofilled_vm_name": form.cleaned_data.get("vm_name", ""),
            "expected_vm_name": disk1.stem,
            "dry_run_vm_name": result.name,
            "dry_run_can_proceed": result.details.get("compatibility", {}).get("can_proceed"),
            "dry_run_summary": result.details.get("compatibility", {}).get("summary"),
            "warnings": result.warnings,
            "warning_count": len(result.warnings),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
