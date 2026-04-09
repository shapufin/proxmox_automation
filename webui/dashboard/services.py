from __future__ import annotations

import json
import logging
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from django.conf import settings
from django.utils import timezone
import yaml

from vmware_to_proxmox.config import AppConfig
from vmware_to_proxmox.engine import MigrationEngine
from vmware_to_proxmox.models import DiskFormat, SourceMode

from .models import MigrationJob, JobStatus, MigrationMode


def get_engine() -> MigrationEngine:
    config = AppConfig.load(settings.MIGRATION_CONFIG_PATH)
    return MigrationEngine(config, logger=logging.getLogger("webui.migration"))


def config_profiles_root() -> Path:
    root = Path(settings.MIGRATION_CONFIG_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_config_profile_path(profile_name: str) -> Path:
    if not profile_name:
        return Path(settings.MIGRATION_CONFIG_PATH)
    profile_path = Path(profile_name)
    if profile_path.suffix.lower() not in {".yaml", ".yml"}:
        profile_path = profile_path.with_suffix(".yaml")
    if not profile_path.is_absolute():
        profile_path = config_profiles_root() / profile_path.name
    profile_path = profile_path.resolve()
    root = config_profiles_root().resolve()
    if root not in profile_path.parents and profile_path != root:
        raise ValueError("Profile path must stay inside the configured config directory")
    return profile_path


def list_config_profiles() -> list[Path]:
    root = config_profiles_root()
    return sorted(path for path in root.glob("*.y*ml") if path.is_file())


def load_config_profile(profile_name: str = "") -> str:
    path = resolve_config_profile_path(profile_name)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def save_config_profile(profile_name: str, content: str) -> Path:
    path = resolve_config_profile_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_load(content or "{}")
    path.write_text(content, encoding="utf-8")
    return path


def config_profile_choices() -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    for profile in list_config_profiles():
        choices.append((profile.stem, profile.stem))
    return choices


def stage_root() -> Path:
    root = Path(settings.MIGRATION_STAGE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_stage_path(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = stage_root() / candidate
    return candidate.resolve()


def list_stage_entries(directory: str = "") -> dict[str, list[Path] | Path]:
    root = stage_root()
    directory_path = resolve_stage_path(directory) if directory else root
    if directory_path.is_file():
        directory_path = directory_path.parent
    entries = sorted(directory_path.iterdir()) if directory_path.exists() else []
    folders = [entry for entry in entries if entry.is_dir()]
    files = [entry for entry in entries if entry.is_file()]

    # Recursively expose files beneath the selected directory so cloned VM
    # layouts that nest the .vmx and .vmdk files in subfolders are discoverable.
    def _walk_files(base: Path) -> list[Path]:
        discovered: list[Path] = []
        try:
            for child in sorted(base.iterdir()):
                if child.is_file():
                    discovered.append(child)
                elif child.is_dir():
                    discovered.extend(_walk_files(child))
        except OSError:
            return []
        return discovered

    for folder in folders:
        files.extend(_walk_files(folder))
    return {"directory": directory_path, "folders": folders, "files": files}


def file_choice_items(files: Iterable[Path]) -> list[tuple[str, str]]:
    return [(str(path), path.name) for path in files]


def _engine_from_job(job: MigrationJob) -> MigrationEngine:
    """Build a MigrationEngine from a registered host pair when available,
    falling back to the config-file path when not."""
    if job.proxmox_host_id:
        from .models import ProxmoxHost, VMwareHost
        from vmware_to_proxmox.config import AppConfig, ProxmoxConfig, VmwareConfig, MigrationConfig
        pve = job.proxmox_host
        vmw = job.vmware_host

        proxmox_cfg = ProxmoxConfig(
            node=pve.node,
            default_storage=pve.default_storage,
            default_bridge=pve.default_bridge,
            ssh_enabled=pve.ssh_enabled,
            ssh_host=pve.ssh_host or pve.api_host,
            ssh_port=pve.ssh_port,
            ssh_username=pve.ssh_username,
            ssh_password=pve.ssh_password,
            ssh_private_key=pve.ssh_private_key,
            api_host=pve.api_host,
            api_user=pve.api_user,
            api_token_name=pve.api_token_name,
            api_token_value=pve.api_token_value,
            api_verify_ssl=pve.api_verify_ssl,
        )
        vmware_cfg = VmwareConfig(
            host=vmw.host if vmw else "",
            username=vmw.username if vmw else "",
            password=vmw.password if vmw else "",
            port=vmw.port if vmw else 443,
            allow_insecure_ssl=vmw.allow_insecure_ssl if vmw else True,
        )
        config = AppConfig(proxmox=proxmox_cfg, vmware=vmware_cfg, migration=MigrationConfig())
        return MigrationEngine(config, logger=logging.getLogger("webui.migration"))

    config_path = resolve_config_profile_path(job.config_profile)
    return MigrationEngine(AppConfig.load(config_path), logger=logging.getLogger("webui.migration"))


def execute_job(job: MigrationJob) -> MigrationJob:
    engine = _engine_from_job(job)
    vm_name = job.vm_name.strip()
    storage = job.storage or None
    bridge = job.bridge or None
    disk_format = DiskFormat(job.disk_format) if job.disk_format else None
    vmid = job.vmid if job.vmid and job.vmid > 0 else None
    log_lines: list[str] = [
        f"Starting job {job.id} ({job.name})",
        f"mode={job.mode}",
        f"vm_name={vm_name}",
        f"vmid={vmid if vmid is not None else 'next_free'}",
        f"storage={storage or ''}",
        f"bridge={bridge or ''}",
        f"disk_format={disk_format.value if disk_format else 'auto'}",
        f"dry_run={job.dry_run}",
        f"start_after_import={job.start_after_import}",
    ]

    try:
        if job.mode == MigrationMode.LOCAL:
            _raw_manifest = (job.manifest_path or "").strip()
            manifest_path = resolve_stage_path(_raw_manifest) if _raw_manifest else None
            disk_paths = [Path(path).expanduser() for path in job.source_paths if path and str(path).strip()]
            log_lines.append(f"manifest_path={manifest_path if manifest_path is not None else 'none'}")
            log_lines.append(f"source_paths={[str(p) for p in disk_paths]}")
            log_lines.append(f"disk_storage_map={json.dumps(job.disk_storage_map or {}, default=str)}")
            log_lines.append(f"nic_bridge_map={json.dumps(job.nic_bridge_map or {}, default=str)}")
            log_lines.append(f"disk_resize_map={json.dumps(job.disk_resize_map or {}, default=str)}")
            result = engine.migrate_local_disks_or_archive(
                vm_name=vm_name,
                manifest_path=manifest_path,
                disk_paths=disk_paths,
                storage=storage,
                bridge=bridge,
                disk_format=disk_format,
                dry_run=job.dry_run,
                start_after_import=job.start_after_import,
                vmx_specs=job.vmx_specs if job.vmx_specs else None,
                vmid=vmid,
                disk_storage_map=job.disk_storage_map or None,
                nic_bridge_map=job.nic_bridge_map or None,
                disk_resize_map=job.disk_resize_map or None,
            )
        else:
            log_lines.append(f"vmx_specs={json.dumps(job.vmx_specs or {}, default=str)}")
            log_lines.append(f"disk_resize_map={json.dumps(job.disk_resize_map or {}, default=str)}")
            result = engine.migrate_vm(
                vm_name=vm_name,
                storage=storage,
                bridge=bridge,
                disk_format=disk_format,
                dry_run=job.dry_run,
                start_after_import=job.start_after_import,
                vmid=vmid,
                disk_storage_map=job.disk_storage_map or None,
                nic_bridge_map=job.nic_bridge_map or None,
                vmx_specs=job.vmx_specs if job.vmx_specs else None,
                disk_resize_map=job.disk_resize_map or None,
            )

        result_payload = json.loads(json.dumps(asdict(result), default=str))
        job.status = JobStatus.SUCCEEDED
        job.result = result_payload
        job.error = ""
        job.finished_at = timezone.now()
        log_lines.append("Migration completed successfully")
        log_lines.append(f"result={json.dumps(result_payload, default=str)}")
        job.logs = "\n".join(log_lines) + "\n"
        job.save(update_fields=["status", "result", "error", "logs", "updated_at", "finished_at"])
        return job
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error = str(exc)
        job.finished_at = timezone.now()
        tb = traceback.format_exc()
        log_lines.append(f"ERROR={type(exc).__name__}: {exc}")
        log_lines.append(tb)
        job.logs = "\n".join(log_lines) + "\n"
        job.save(update_fields=["status", "error", "logs", "updated_at", "finished_at"])
        raise
