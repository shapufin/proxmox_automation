from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0010_migrationjob_allow_disk_shrink_fallback_nic_bridge"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="migration_ledger",
            field=models.JSONField(
                blank=True,
                default={
                    "schema_version": 1,
                    "stages": {
                        "vm_created": {
                            "status": "pending",
                            "started_at": None,
                            "completed_at": None,
                            "error": "",
                            "artifacts": {
                                "vmid": None,
                                "proxmox_name": "",
                            },
                        },
                        "disks_exported": {
                            "status": "pending",
                            "started_at": None,
                            "completed_at": None,
                            "error": "",
                            "artifacts": {
                                "source_paths": [],
                                "export_paths": [],
                            },
                        },
                        "disks_imported": {
                            "status": "pending",
                            "started_at": None,
                            "completed_at": None,
                            "error": "",
                            "artifacts": {
                                "volume_ids": [],
                                "imported_disks": [],
                            },
                        },
                        "nics_configured": {
                            "status": "pending",
                            "started_at": None,
                            "completed_at": None,
                            "error": "",
                            "artifacts": {
                                "networks": [],
                            },
                        },
                        "remediation_applied": {
                            "status": "pending",
                            "started_at": None,
                            "completed_at": None,
                            "error": "",
                            "artifacts": {
                                "script_path": "",
                                "applied": False,
                            },
                        },
                    },
                    "cleanup": {
                        "status": "pending",
                        "started_at": None,
                        "completed_at": None,
                        "deleted_volume_ids": [],
                        "deleted_vmid": None,
                        "errors": [],
                    },
                },
            ),
        ),
    ]
