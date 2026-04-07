from __future__ import annotations

import json
import logging
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
    return {"directory": directory_path, "folders": folders, "files": files}


def file_choice_items(files: Iterable[Path]) -> list[tuple[str, str]]:
    return [(str(path), path.name) for path in files]


def execute_job(job: MigrationJob) -> MigrationJob:
    config_path = resolve_config_profile_path(job.config_profile)
    engine = MigrationEngine(AppConfig.load(config_path), logger=logging.getLogger("webui.migration"))
    vm_name = job.vm_name.strip()
    storage = job.storage or None
    bridge = job.bridge or None
    disk_format = DiskFormat(job.disk_format) if job.disk_format else None

    if job.mode == MigrationMode.LOCAL:
        manifest_path = resolve_stage_path(job.manifest_path) if job.manifest_path else Path("")
        disk_paths = [resolve_stage_path(path) for path in job.source_paths]
        result = engine.migrate_local_disks_or_archive(
            vm_name=vm_name,
            manifest_path=manifest_path,
            disk_paths=disk_paths,
            storage=storage,
            bridge=bridge,
            disk_format=disk_format,
            dry_run=job.dry_run,
            start_after_import=job.start_after_import,
        )
    else:
        result = engine.migrate_vm(
            vm_name=vm_name,
            storage=storage,
            bridge=bridge,
            disk_format=disk_format,
            dry_run=job.dry_run,
            start_after_import=job.start_after_import,
        )

    job.status = JobStatus.SUCCEEDED
    job.result = json.loads(json.dumps(asdict(result), default=str))
    job.error = ""
    job.finished_at = timezone.now()
    job.logs = f"Migration completed successfully for {job.name}\n"
    job.save(update_fields=["status", "result", "error", "logs", "updated_at", "finished_at"])
    return job
