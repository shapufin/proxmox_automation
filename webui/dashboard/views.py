from __future__ import annotations

import json
from pathlib import Path

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from vmware_to_proxmox.models import DiskFormat

from .forms import ConfigProfileForm, DiskBrowseForm, MigrationJobForm
from .models import JobStatus, MigrationJob, MigrationMode
from .services import (
    config_profile_choices,
    execute_job,
    file_choice_items,
    get_engine,
    list_config_profiles,
    list_stage_entries,
    load_config_profile,
    resolve_config_profile_path,
    resolve_stage_path,
    save_config_profile,
)


def _empty_inventory() -> dict[str, list[object]]:
    return {
        "vmware_vms": [],
        "proxmox_storages": [],
        "proxmox_bridges": [],
    }


def _job_form(directory: str = "", vm_choices: list[tuple[str, str]] | None = None) -> MigrationJobForm:
    form = MigrationJobForm(initial={"directory": directory})
    stage_view = list_stage_entries(directory)
    form.set_source_choices(file_choice_items(stage_view["files"]))
    form.set_config_profile_choices(config_profile_choices())
    form.set_vm_choices(vm_choices or [])
    return form


def _proxmox_choice_items(inventory: dict[str, object]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    storage_choices: list[tuple[str, str]] = []
    for item in inventory.get("proxmox_storages", []):
        if isinstance(item, dict):
            storage_name = str(item.get("storage", "")).strip()
            if storage_name:
                label = storage_name
                content = str(item.get("content", "")).strip()
                if content:
                    label = f"{storage_name} ({content})"
                storage_choices.append((storage_name, label))

    bridge_choices: list[tuple[str, str]] = []
    for item in inventory.get("proxmox_bridges", []):
        if isinstance(item, dict):
            bridge_name = str(item.get("name", "")).strip()
            if bridge_name:
                label = bridge_name
                if item.get("vlan_aware"):
                    label = f"{bridge_name} (VLAN aware)"
                bridge_choices.append((bridge_name, label))

    return storage_choices, bridge_choices


def _config_form(profile_name: str = "") -> ConfigProfileForm:
    content = load_config_profile(profile_name)
    form = ConfigProfileForm(initial={"name": profile_name, "content": content})
    return form


def _render_dashboard(
    request: HttpRequest,
    form: MigrationJobForm,
    inventory: dict[str, object],
    config_form: ConfigProfileForm | None = None,
) -> HttpResponse:
    storage_choices, bridge_choices = _proxmox_choice_items(inventory)
    form.set_storage_choices(storage_choices)
    form.set_bridge_choices(bridge_choices)
    return render(
        request,
        "dashboard/index.html",
        {
            "inventory": inventory,
            "jobs": MigrationJob.objects.all()[:20],
            "form": form,
            "config_profiles": list_config_profiles(),
            "config_form": config_form or _config_form(),
        },
    )


def dashboard(request: HttpRequest) -> HttpResponse:
    directory = request.GET.get("directory", "")
    profile_name = request.GET.get("profile", "")
    inventory = _empty_inventory()
    try:
        engine = get_engine()
        inventory = engine.inventory()
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if "Name or service not known" in err or "nodename nor servname" in err or "Connect call failed" in err:
            messages.warning(
                request,
                "Proxmox host is not reachable yet. Set proxmox.api_host (IP address) and "
                "proxmox.ssh_host in config.yaml, then click \u22ef Test & Refresh.",
            )
        else:
            messages.warning(request, f"Inventory is unavailable right now: {exc}")
    vm_choices = [(vm, vm) for vm in inventory.get("vmware_vms", [])]
    return _render_dashboard(request, _job_form(directory, vm_choices), inventory, _config_form(profile_name))


def browse_disks(request: HttpRequest) -> HttpResponse:
    form = DiskBrowseForm(request.GET or None)
    directory = form.data.get("directory", "") if form.is_bound else ""
    stage_view = list_stage_entries(directory)
    files = stage_view["files"]
    nested_dirs = stage_view["folders"]
    return render(
        request,
        "dashboard/browse.html",
        {
            "form": form,
            "directory": str(stage_view["directory"]),
            "folders": nested_dirs,
            "files": files,
            "file_choices": file_choice_items(files),
        },
    )


def launch_job(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("dashboard:index")

    inventory = _empty_inventory()
    try:
        inventory = get_engine().inventory()
    except Exception as exc:  # noqa: BLE001
        messages.warning(request, f"Inventory is unavailable right now: {exc}")
    vm_choices = [(vm, vm) for vm in inventory.get("vmware_vms", [])]
    form = MigrationJobForm(request.POST)
    directory = request.POST.get("directory", "")
    stage_view = list_stage_entries(directory)
    form.set_source_choices(file_choice_items(stage_view["files"]))
    form.set_config_profile_choices(config_profile_choices())
    form.set_vm_choices(vm_choices)
    storage_choices, bridge_choices = _proxmox_choice_items(inventory)
    form.set_storage_choices(storage_choices)
    form.set_bridge_choices(bridge_choices)

    if form.is_valid():
        job = MigrationJob.objects.create(
            name=form.cleaned_data["name"],
            mode=form.cleaned_data["mode"],
            config_profile=form.cleaned_data.get("config_profile", ""),
            vm_name=form.cleaned_data.get("vm_name", ""),
            manifest_path=form.cleaned_data.get("manifest_path", ""),
            source_paths=form.normalized_source_paths(),
            storage=form.cleaned_data.get("storage", ""),
            bridge=form.cleaned_data.get("bridge", ""),
            disk_format=form.cleaned_data.get("disk_format", ""),
            dry_run=bool(form.cleaned_data.get("dry_run", False)),
            start_after_import=bool(form.cleaned_data.get("start_after_import", True)),
            status=JobStatus.PENDING,
        )
        messages.success(request, f"Created job {job.id}. The worker will pick it up.")
        return redirect("dashboard:job_detail", job_id=job.id)

    messages.error(request, "Please fix the highlighted errors.")
    config_form = _config_form(form.cleaned_data.get("config_profile", "") if hasattr(form, "cleaned_data") else "")
    return _render_dashboard(request, form, inventory, config_form)


def config_profile_editor(request: HttpRequest) -> HttpResponse:
    selected = request.GET.get("profile", "")
    return render(
        request,
        "dashboard/config_editor.html",
        {
            "profiles": list_config_profiles(),
            "form": _config_form(selected),
        },
    )


def save_profile(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("dashboard:config_profiles")
    form = ConfigProfileForm(request.POST)
    if form.is_valid():
        try:
            path = save_config_profile(form.cleaned_data["name"], form.cleaned_data["content"])
            messages.success(request, f"Saved profile {path.name}.")
            return redirect("dashboard:config_profiles")
        except Exception as exc:  # noqa: BLE001
            form.add_error(None, str(exc))
    return render(
        request,
        "dashboard/config_editor.html",
        {
            "profiles": list_config_profiles(),
            "form": form,
        },
    )


def job_detail(request: HttpRequest, job_id: int) -> HttpResponse:
    job = get_object_or_404(MigrationJob, pk=job_id)
    return render(request, "dashboard/job_detail.html", {"job": job})


def run_pending_job(request: HttpRequest, job_id: int) -> HttpResponse:
    job = get_object_or_404(MigrationJob, pk=job_id)
    if job.status != JobStatus.PENDING:
        messages.info(request, "This job is not pending.")
        return redirect("dashboard:job_detail", job_id=job.id)
    job.status = JobStatus.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at", "updated_at"])
    try:
        execute_job(job)
        messages.success(request, f"Job {job.id} completed.")
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = str(exc)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at", "updated_at"])
        messages.error(request, f"Job {job.id} failed: {exc}")
    return redirect("dashboard:job_detail", job_id=job.id)


@require_GET
def proxmox_status(request: HttpRequest) -> JsonResponse:
    """Live Proxmox connectivity check — returns storages, bridges, and any error."""
    try:
        engine = get_engine()
        storages = engine.proxmox.list_storages()
        bridges = engine.proxmox.list_bridges()
        return JsonResponse({
            "ok": True,
            "storages": [
                {
                    "storage": s.storage,
                    "content": s.content,
                    "type": s.storage_type,
                    "active": s.active,
                    "available_gb": round(s.available / (1024 ** 3), 1) if s.available else 0,
                }
                for s in storages
            ],
            "bridges": [
                {
                    "name": b.name,
                    "active": b.active,
                    "vlan_aware": b.vlan_aware,
                    "ports": b.bridge_ports,
                }
                for b in bridges
            ],
        })
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(exc)}, status=200)


@require_GET
def browse_directory(request: HttpRequest) -> JsonResponse:
    """List HOST filesystem via SFTP when SSH is configured, otherwise local."""
    raw = request.GET.get("path", "")
    if not raw:
        raw = "/"
    try:
        engine = get_engine()
        result = engine.proxmox.list_remote_dir(raw)
        return JsonResponse({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(exc)}, status=200)


@require_GET
def vmdk_scan(request: HttpRequest) -> JsonResponse:
    """Scan a HOST directory for VMDK files and auto-detect manifest.json.
    Returns {ok, path, vmdks:[{path,name,size}], manifest_path, manifest}."""
    raw = request.GET.get("path", "")
    if not raw:
        return JsonResponse({"ok": False, "error": "path is required"}, status=200)
    try:
        engine = get_engine()
        listing = engine.proxmox.list_remote_dir(raw)
        all_files = listing.get("files", [])
        vmdks = [f for f in all_files if f["name"].lower().endswith(".vmdk")]
        manifest_path = ""
        manifest = {}
        for f in all_files:
            if f["name"].lower() == "manifest.json":
                manifest_path = f["path"]
                try:
                    content = engine.proxmox.read_remote_file(manifest_path)
                    manifest = json.loads(content)
                except Exception:  # noqa: BLE001
                    manifest = {}
                break
        return JsonResponse({
            "ok": True,
            "path": listing["path"],
            "vmdks": vmdks,
            "manifest_path": manifest_path,
            "manifest": manifest,
        })
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(exc)}, status=200)
