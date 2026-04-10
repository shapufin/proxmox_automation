from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import click

from .config import AppConfig
from .engine import MigrationEngine
from .logger import setup_logging
from .models import DiskFormat, SourceMode


def _resolve_config_path(config_path: Optional[Path], config_dir: Optional[Path], config_profile: Optional[str]) -> Path:
    if config_profile:
        base_dir = config_dir or Path("configs")
        profile_path = Path(config_profile)
        if profile_path.suffix.lower() not in {".yaml", ".yml"}:
            profile_path = profile_path.with_suffix(".yaml")
        if not profile_path.is_absolute():
            profile_path = base_dir / profile_path.name
        return profile_path
    if config_path is None:
        raise click.ClickException("Either --config or --config-profile must be provided")
    return config_path


def _engine_from_config(config_path: Path, verbose: bool) -> MigrationEngine:
    config = AppConfig.load(config_path)
    logger = setup_logging(verbose)
    return MigrationEngine(config, logger=logger)


def _echo_result(result) -> None:
    click.echo(
        json.dumps(
            {
                "name": result.name,
                "vmid": result.vmid,
                "storage": result.target_storage,
                "disk_format": result.disk_format.value,
                "firmware": result.firmware.value,
                "warnings": result.warnings,
                "details": result.details,
            },
            indent=2,
            sort_keys=True,
        )
    )


@click.group()
def cli() -> None:
    pass


@cli.command()
@click.option("--config", "config_path", type=click.Path(exists=False, dir_okay=False, path_type=Path), default=None, help="Path to a YAML config file.")
@click.option("--config-dir", type=click.Path(exists=False, file_okay=False, path_type=Path), default=None, help="Directory containing config profiles.")
@click.option("--config-profile", type=str, default=None, help="Config profile name inside the config directory.")
@click.option("--verbose", is_flag=True, default=False)
@click.pass_context
def inventory(ctx: click.Context, config_path: Optional[Path], config_dir: Optional[Path], config_profile: Optional[str], verbose: bool) -> None:
    resolved_config = _resolve_config_path(config_path, config_dir, config_profile)
    ctx.obj = {
        "config": AppConfig.load(resolved_config),
        "logger": setup_logging(verbose),
    }
    engine = MigrationEngine(ctx.obj["config"], logger=ctx.obj["logger"])
    payload = engine.inventory()
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@cli.command()
@click.option("--config", "config_path", type=click.Path(exists=False, dir_okay=False, path_type=Path), default=None, help="Path to a YAML config file.")
@click.option("--config-dir", type=click.Path(exists=False, file_okay=False, path_type=Path), default=None, help="Directory containing config profiles.")
@click.option("--config-profile", type=str, default=None, help="Config profile name inside the config directory.")
@click.option("--verbose", is_flag=True, default=False)
@click.option("--source-mode", type=click.Choice([item.value for item in SourceMode], case_sensitive=False), default=SourceMode.VMWARE.value, show_default=True)
@click.option("--vm", "vm_name", default=None, help="VM name to plan or override the manifest name.")
@click.option("--manifest", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Local manifest JSON exported by the migration tool.")
@click.option("--disk-path", "disk_paths", multiple=True, type=click.Path(path_type=Path), help="One or more local disk files or directories.")
@click.option("--disk-dir", "disk_dirs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path), help="A local directory containing one or more disk files.")
@click.option("--storage", default=None, help="Override Proxmox target storage.")
@click.option("--bridge", default=None, help="Override the target bridge for all NICs.")
@click.option("--format", "disk_format", type=click.Choice([item.value for item in DiskFormat], case_sensitive=False), default=None)
@click.pass_context
def plan(
    ctx: click.Context,
    config_path: Optional[Path],
    config_dir: Optional[Path],
    config_profile: Optional[str],
    verbose: bool,
    source_mode: str,
    vm_name: Optional[str],
    manifest: Optional[Path],
    disk_paths: tuple[Path, ...],
    disk_dirs: tuple[Path, ...],
    storage: Optional[str],
    bridge: Optional[str],
    disk_format: Optional[str],
) -> None:
    engine = _engine_from_config(_resolve_config_path(config_path, config_dir, config_profile), verbose)
    target_format = DiskFormat(disk_format) if disk_format else None
    mode = SourceMode(source_mode)

    if mode == SourceMode.LOCAL:
        if manifest is None:
            raise click.ClickException("--manifest is required when --source-mode local is used")
        local_paths = tuple(disk_paths) + tuple(disk_dirs)
        result = engine.migrate_local_disks(
            vm_name=vm_name or "",
            manifest_path=manifest,
            disk_paths=local_paths,
            storage=storage,
            bridge=bridge,
            disk_format=target_format,
            dry_run=True,
            start_after_import=False,
            write_manifest=False,
        )
        _echo_result(result)
        return

    if not vm_name:
        raise click.ClickException("--vm is required when --source-mode vmware is used")

    plan_result = engine.build_plan(
        vm_name=vm_name,
        storage=storage,
        bridge=bridge,
        disk_format=target_format,
    )
    click.echo(json.dumps({
        "vm_name": plan_result.vm_name,
        "vmid": plan_result.vmid,
        "storage": plan_result.storage,
        "bridge": plan_result.bridge,
        "disk_format": plan_result.disk_format.value,
        "firmware": plan_result.firmware.value,
        "warnings": plan_result.warnings,
        "nics": plan_result.nics,
        "disks": plan_result.disks,
        "compatibility": plan_result.compatibility,
    }, indent=2, sort_keys=True))


@cli.command()
@click.option("--config", "config_path", type=click.Path(exists=False, dir_okay=False, path_type=Path), default=None, help="Path to a YAML config file.")
@click.option("--config-dir", type=click.Path(exists=False, file_okay=False, path_type=Path), default=None, help="Directory containing config profiles.")
@click.option("--config-profile", type=str, default=None, help="Config profile name inside the config directory.")
@click.option("--verbose", is_flag=True, default=False)
@click.option("--source-mode", type=click.Choice([item.value for item in SourceMode], case_sensitive=False), default=SourceMode.VMWARE.value, show_default=True)
@click.option("--vm", "vm_names", multiple=True, help="VM name to migrate. Repeat for batch mode.")
@click.option("--manifest", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Local manifest JSON exported by the migration tool.")
@click.option("--disk-path", "disk_paths", multiple=True, type=click.Path(path_type=Path), help="One or more local disk files or directories.")
@click.option("--disk-dir", "disk_dirs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path), help="A local directory containing one or more disk files.")
@click.option("--storage", default=None, help="Override Proxmox target storage.")
@click.option("--bridge", default=None, help="Override the target bridge for all NICs.")
@click.option("--format", "disk_format", type=click.Choice([item.value for item in DiskFormat], case_sensitive=False), default=None)
@click.option("--dry-run/--execute", default=None, help="Preview the migration without changing Proxmox.")
@click.option("--no-start", is_flag=True, default=False, help="Do not start the VM after import.")
@click.pass_context
def migrate(
    ctx: click.Context,
    config_path: Optional[Path],
    config_dir: Optional[Path],
    config_profile: Optional[str],
    verbose: bool,
    source_mode: str,
    vm_names: tuple[str, ...],
    manifest: Optional[Path],
    disk_paths: tuple[Path, ...],
    disk_dirs: tuple[Path, ...],
    storage: Optional[str],
    bridge: Optional[str],
    disk_format: Optional[str],
    dry_run: Optional[bool],
    no_start: bool,
) -> None:
    engine = _engine_from_config(_resolve_config_path(config_path, config_dir, config_profile), verbose)
    format_override = DiskFormat(disk_format) if disk_format else None
    mode = SourceMode(source_mode)

    if mode == SourceMode.LOCAL:
        if manifest is None:
            raise click.ClickException("--manifest is required when --source-mode local is used")
        local_paths = tuple(disk_paths) + tuple(disk_dirs)
        result = engine.migrate_local_disks(
            vm_name=vm_names[0] if vm_names else "",
            manifest_path=manifest,
            disk_paths=local_paths,
            storage=storage,
            bridge=bridge,
            disk_format=format_override,
            dry_run=dry_run,
            start_after_import=not no_start,
        )
        _echo_result(result)
        return

    if not vm_names:
        available = engine.inventory()["vmware_vms"]
        if not available:
            raise click.ClickException("No VMware VMs found")
        vm_names = (click.prompt("Enter VM name to migrate", type=click.Choice(available)),)

    for vm_name in vm_names:
        result = engine.migrate_vm(
            vm_name=vm_name,
            storage=storage,
            bridge=bridge,
            disk_format=format_override,
            dry_run=dry_run,
            start_after_import=not no_start,
        )
        _echo_result(result)
