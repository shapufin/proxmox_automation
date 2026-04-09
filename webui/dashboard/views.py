from __future__ import annotations

import json
from pathlib import Path

from django.contrib import messages
from django.core.cache import cache as _django_cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from vmware_to_proxmox.models import DiskFormat

from .forms import ConfigProfileForm, DiskBrowseForm, MigrationJobForm, ProxmoxHostForm, VMwareHostForm
from .models import JobStatus, MigrationJob, MigrationMode, ProxmoxHost, VMwareHost
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

_PROXMOX_STATUS_TTL = 30  # seconds
_PROXMOX_STATUS_CACHE_KEY = "proxmox_status_v1"


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
        # Query Proxmox first (storages + bridges) independently of VMware
        try:
            inventory["proxmox_storages"] = [
                {"storage": s.storage, "content": s.content, "type": s.storage_type, "active": s.active}
                for s in engine.proxmox.list_storages()
            ]
            inventory["proxmox_bridges"] = [
                {"name": b.name, "active": b.active, "vlan_aware": b.vlan_aware}
                for b in engine.proxmox.list_bridges()
            ]
        except Exception as pve_exc:  # noqa: BLE001
            err = str(pve_exc)
            if any(k in err for k in ("Name or service not known", "nodename nor servname", "Connect call failed", "Connection refused", "timed out")):
                messages.warning(
                    request,
                    "Proxmox host is not reachable yet. Set proxmox.api_host (IP address) and "
                    "proxmox.ssh_host in config.yaml, then click \u22ef Test & Refresh.",
                )
            else:
                messages.warning(request, f"Proxmox inventory unavailable: {pve_exc}")
        # VMware VMs — failure is silent (VMware may not be configured)
        try:
            with engine.vmware:
                inventory["vmware_vms"] = engine.vmware.list_vms()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        messages.warning(request, f"Configuration error: {exc}")
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
        engine = get_engine()
        try:
            inventory["proxmox_storages"] = [
                {"storage": s.storage, "content": s.content, "type": s.storage_type, "active": s.active}
                for s in engine.proxmox.list_storages()
            ]
            inventory["proxmox_bridges"] = [
                {"name": b.name, "active": b.active, "vlan_aware": b.vlan_aware}
                for b in engine.proxmox.list_bridges()
            ]
        except Exception as pve_exc:  # noqa: BLE001
            messages.warning(request, f"Proxmox inventory unavailable: {pve_exc}")
        try:
            with engine.vmware:
                inventory["vmware_vms"] = engine.vmware.list_vms()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        messages.warning(request, f"Configuration error: {exc}")
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
        def _parse_json_field(raw: str) -> dict:
            try:
                v = json.loads(raw or "{}")
                return v if isinstance(v, dict) else {}
            except Exception:
                return {}
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
            disk_storage_map=_parse_json_field(form.cleaned_data.get("disk_storage_map", "")),
            nic_bridge_map=_parse_json_field(form.cleaned_data.get("nic_bridge_map", "")),
            vmx_specs=_parse_json_field(form.cleaned_data.get("vmx_specs", "")),
            proxmox_host=ProxmoxHost.objects.filter(pk=form.cleaned_data.get("proxmox_host_id") or 0).first(),
            vmware_host=VMwareHost.objects.filter(pk=form.cleaned_data.get("vmware_host_id") or 0).first(),
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


def job_list(request: HttpRequest) -> HttpResponse:
    jobs = MigrationJob.objects.all()[:100]
    return render(request, "dashboard/job_list.html", {"jobs": jobs})


def job_detail(request: HttpRequest, job_id: int) -> HttpResponse:
    job = get_object_or_404(MigrationJob, pk=job_id)
    return render(request, "dashboard/job_detail.html", {"job": job})


@require_GET
def job_status_api(request: HttpRequest, job_id: int) -> JsonResponse:
    job = get_object_or_404(MigrationJob, pk=job_id)
    return JsonResponse({
        "status": job.status,
        "logs": job.logs,
        "error": job.error,
        "result": job.result,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    })


def run_pending_job(request: HttpRequest, job_id: int) -> HttpResponse:
    from django.db import transaction
    with transaction.atomic():
        job = get_object_or_404(MigrationJob.objects.select_for_update(), pk=job_id)
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


def delete_job(request: HttpRequest, job_id: int) -> HttpResponse:
    """Delete a migration job (POST only, with confirmation)."""
    job = get_object_or_404(MigrationJob, pk=job_id)
    if request.method == "POST":
        job.delete()
        messages.success(request, f"Job #{job_id} deleted.")
        return redirect("dashboard:job_list")
    # GET: confirmation page
    return render(request, "dashboard/job_confirm_delete.html", {"job": job})


@require_GET
def proxmox_status(request: HttpRequest) -> JsonResponse:
    """Live Proxmox connectivity check — returns storages, bridges, and any error.
    Pass ?force=1 to bypass the 30-second cache and force a fresh connection."""
    force = request.GET.get("force", "") in {"1", "true"}
    if not force:
        cached = _django_cache.get(_PROXMOX_STATUS_CACHE_KEY)
        if cached:
            return JsonResponse(cached)
    try:
        engine = get_engine()
        if force:
            engine.proxmox.reset()  # drop stale API + SSH connections
        storages = engine.proxmox.list_storages()
        bridges = engine.proxmox.list_bridges()
        payload: dict[str, object] = {
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
        }
        _django_cache.set(_PROXMOX_STATUS_CACHE_KEY, payload, timeout=_PROXMOX_STATUS_TTL)
        return JsonResponse(payload)
    except Exception as exc:  # noqa: BLE001
        _django_cache.delete(_PROXMOX_STATUS_CACHE_KEY)
        return JsonResponse({"ok": False, "error": str(exc)}, status=200)


@require_GET
def vmware_vms(request: HttpRequest) -> JsonResponse:
    """Return the list of VMware VMs available for direct migration."""
    try:
        engine = get_engine()
        vms = engine.inventory().get("vmware_vms", [])
        return JsonResponse({"ok": True, "vms": list(vms)})
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(exc), "vms": []}, status=200)


def wizard(request: HttpRequest) -> HttpResponse:
    """Dedicated migration wizard page."""
    profile_name = request.GET.get("profile", "")
    return render(
        request,
        "dashboard/wizard.html",
        {
            "config_profiles":  list_config_profiles(),
            "selected_profile": profile_name,
            "disk_format_choices": [("", "Auto (qcow2)"), ("qcow2", "qcow2"), ("raw", "raw")],
            "proxmox_hosts": list(ProxmoxHost.objects.values("id", "label", "node", "api_host", "default_storage", "default_bridge")),
            "vmware_hosts":  list(VMwareHost.objects.values("id", "label", "host")),
        },
    )


@require_GET
def browse_directory(request: HttpRequest) -> JsonResponse:
    """List HOST filesystem via SFTP when SSH is configured, otherwise local."""
    raw = request.GET.get("path", "")
    if not raw:
        # Default to Proxmox dump directory when browsing host
        raw = "/var/lib/vz/dump"
    try:
        engine = get_engine()
        result = engine.proxmox.list_remote_dir(raw)
        return JsonResponse({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(exc)}, status=200)


@require_GET
def vmdk_scan(request: HttpRequest) -> JsonResponse:
    """Scan a HOST directory for VMDK files, manifest.json, .vmx specs, and archives.

    Returns:
        ok, path, vmdks, manifest_path, manifest,
        vmx_path, vmx_specs (parsed hardware hints),
        archives ([{path, name, archive_type}])
    """
    from vmware_to_proxmox.disk import detect_archive_type, parse_vmx

    raw = request.GET.get("path", "")
    if not raw:
        return JsonResponse({"ok": False, "error": "path is required"}, status=200)
    try:
        engine = get_engine()
        listing = engine.proxmox.list_remote_dir(raw)
        all_files = listing.get("files", [])

        vmdks: list[dict] = []
        manifest_path = ""
        manifest: dict = {}
        vmx_path = ""
        vmx_specs: dict = {}
        archives: list[dict] = []

        for f in all_files:
            name_lower = f["name"].lower()

            if name_lower.endswith(".vmdk"):
                vmdks.append(f)

            elif name_lower == "manifest.json":
                manifest_path = f["path"]
                try:
                    content = engine.proxmox.read_remote_file(manifest_path)
                    manifest = json.loads(content)
                except Exception:  # noqa: BLE001
                    manifest = {}

            elif name_lower.endswith(".vmx") and not vmx_path:
                vmx_path = f["path"]
                try:
                    content = engine.proxmox.read_remote_file(vmx_path)
                    parsed = parse_vmx(content)
                    # Exclude the raw key-dump from the API response (too large)
                    vmx_specs = {k: v for k, v in parsed.items() if k != "raw"}
                except Exception:  # noqa: BLE001
                    vmx_specs = {}

            else:
                atype = detect_archive_type(f["name"])
                if atype:
                    archives.append({
                        "path": f["path"],
                        "name": f["name"],
                        "size": f.get("size", 0),
                        "archive_type": atype,
                    })

        return JsonResponse({
            "ok": True,
            "path": listing["path"],
            "vmdks": vmdks,
            "manifest_path": manifest_path,
            "manifest": manifest,
            "vmx_path": vmx_path,
            "vmx_specs": vmx_specs,
            "archives": archives,
        })
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(exc)}, status=200)


@require_GET
def peek_archive(request: HttpRequest) -> JsonResponse:
    """List contents of a remote archive via SSH and return VMX specs + VMDK names."""
    archive_path = request.GET.get("path", "").strip()
    if not archive_path:
        return JsonResponse({"ok": False, "error": "path parameter required"})
    try:
        engine = get_engine()
        filenames = engine.proxmox.peek_archive(archive_path)
        vmdks: list[str] = []
        vmx_specs: dict = {}
        for name in filenames:
            lower = name.lower()
            if lower.endswith(".vmdk") and "-flat" not in lower:
                vmdks.append(name)
            elif lower.endswith(".vmx") and not vmx_specs:
                # Try to read the VMX from inside the archive
                # Only feasible for zip; for others we skip (too complex without extract)
                try:
                    from vmware_to_proxmox.disk import parse_vmx, detect_archive_type
                    atype = detect_archive_type(archive_path)
                    if atype == "zip":
                        out = engine.proxmox._run(["unzip", "-p", archive_path, name])
                        parsed = parse_vmx(out)
                        vmx_specs = {k: v for k, v in parsed.items() if k != "raw"}
                    elif atype == "7z":
                        out = engine.proxmox._run(["7z", "e", "-so", archive_path, name])
                        parsed = parse_vmx(out)
                        vmx_specs = {k: v for k, v in parsed.items() if k != "raw"}
                except Exception:  # noqa: BLE001
                    pass
        return JsonResponse({"ok": True, "filenames": filenames, "vmdks": vmdks, "vmx_specs": vmx_specs})
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": str(exc)})


# ─────────────────────────────────────────────────────────────────────────────
# Host management views
# ─────────────────────────────────────────────────────────────────────────────

def hosts(request: HttpRequest) -> HttpResponse:
    return render(request, "dashboard/hosts.html", {
        "proxmox_hosts":     ProxmoxHost.objects.all(),
        "vmware_hosts":      VMwareHost.objects.all(),
        "proxmox_edit":      ProxmoxHostForm(),
        "vmware_edit":       VMwareHostForm(),
        "show_proxmox_form": False,
        "show_vmware_form":  False,
    })


def edit_proxmox_host(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_object_or_404(ProxmoxHost, pk=host_id)
    return render(request, "dashboard/hosts.html", {
        "proxmox_hosts":     ProxmoxHost.objects.all(),
        "vmware_hosts":      VMwareHost.objects.all(),
        "proxmox_edit":      ProxmoxHostForm(instance=host),
        "vmware_edit":       VMwareHostForm(),
        "show_proxmox_form": True,
        "show_vmware_form":  False,
    })


def edit_vmware_host(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_object_or_404(VMwareHost, pk=host_id)
    return render(request, "dashboard/hosts.html", {
        "proxmox_hosts":     ProxmoxHost.objects.all(),
        "vmware_hosts":      VMwareHost.objects.all(),
        "proxmox_edit":      ProxmoxHostForm(),
        "vmware_edit":       VMwareHostForm(instance=host),
        "show_proxmox_form": False,
        "show_vmware_form":  True,
    })


@require_POST
def save_proxmox_host(request: HttpRequest) -> HttpResponse:
    host_id = request.POST.get("host_id")
    instance = get_object_or_404(ProxmoxHost, pk=host_id) if host_id else None
    form = ProxmoxHostForm(request.POST, instance=instance)
    if form.is_valid():
        form.save()
        messages.success(request, f"Proxmox host '{form.cleaned_data['label']}' saved.")
        return redirect("dashboard:hosts")
    return render(request, "dashboard/hosts.html", {
        "proxmox_hosts":     ProxmoxHost.objects.all(),
        "vmware_hosts":      VMwareHost.objects.all(),
        "proxmox_edit":      form,
        "vmware_edit":       VMwareHostForm(),
        "show_proxmox_form": True,
        "show_vmware_form":  False,
    })


@require_POST
def save_vmware_host(request: HttpRequest) -> HttpResponse:
    host_id = request.POST.get("host_id")
    instance = get_object_or_404(VMwareHost, pk=host_id) if host_id else None
    form = VMwareHostForm(request.POST, instance=instance)
    if form.is_valid():
        form.save()
        messages.success(request, f"VMware host '{form.cleaned_data['label']}' saved.")
        return redirect("dashboard:hosts")
    return render(request, "dashboard/hosts.html", {
        "proxmox_hosts":     ProxmoxHost.objects.all(),
        "vmware_hosts":      VMwareHost.objects.all(),
        "proxmox_edit":      ProxmoxHostForm(),
        "vmware_edit":       form,
        "show_proxmox_form": False,
        "show_vmware_form":  True,
    })


@require_POST
def delete_proxmox_host(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_object_or_404(ProxmoxHost, pk=host_id)
    label = host.label
    host.delete()
    messages.success(request, f"Proxmox host '{label}' deleted.")
    return redirect("dashboard:hosts")


@require_POST
def delete_vmware_host(request: HttpRequest, host_id: int) -> HttpResponse:
    host = get_object_or_404(VMwareHost, pk=host_id)
    label = host.label
    host.delete()
    messages.success(request, f"VMware host '{label}' deleted.")
    return redirect("dashboard:hosts")


def test_proxmox_host(request: HttpRequest, host_id: int) -> HttpResponse:
    from vmware_to_proxmox.proxmox import ProxmoxClient
    host = get_object_or_404(ProxmoxHost, pk=host_id)
    
    # Log connection details for debugging
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Testing Proxmox host connection: {host.label}")
    logger.info(f"  API Host: {host.api_host}")
    logger.info(f"  SSH Host: {host.ssh_host}")
    logger.info(f"  SSH Enabled: {host.ssh_enabled}")
    logger.info(f"  Node: {host.node}")
    
    client = ProxmoxClient(
        node=host.node,
        ssh_enabled=host.ssh_enabled,
        ssh_host=host.ssh_host or host.api_host,
        ssh_port=host.ssh_port,
        ssh_username=host.ssh_username,
        ssh_private_key=host.ssh_private_key,
        ssh_password=host.ssh_password,
        api_host=host.api_host,
        api_user=host.api_user,
        api_token_name=host.api_token_name,
        api_token_value=host.api_token_value,
        api_verify_ssl=host.api_verify_ssl,
    )
    
    try:
        # Test prerequisites first
        client.ensure_prerequisites()
        
        # Test API connection
        api = client._api_client()
        if api is None:
            messages.warning(request, f"⚠ API connection failed to '{host.label}', trying SSH only...")
        
        # Test storage discovery
        storages = client.list_storages()
        
        # Test bridge discovery (this will test SDN discovery too)
        bridges = client.list_bridges()
        
        # Success message with details
        success_msg = f"✅ Connected to '{host.label}': {len(storages)} storage(s), {len(bridges)} network(s)."
        
        # Add details about what was found
        if api:
            success_msg += " (API + SSH)"
        else:
            success_msg += " (SSH only)"
            
        # List some details
        if storages:
            storage_names = [s.storage for s in storages[:3]]
            success_msg += f" Storages: {', '.join(storage_names)}{'...' if len(storages) > 3 else ''}"
            
        if bridges:
            bridge_names = [b.name for b in bridges[:3]]
            success_msg += f" Networks: {', '.join(bridge_names)}{'...' if len(bridges) > 3 else ''}"
        
        messages.success(request, success_msg)
        
    except Exception as exc:  # noqa: BLE001
        # Better error messages
        error_msg = str(exc)
        
        # Common issues and their solutions
        if "Name or service not known" in error_msg:
            error_msg += " → Check DNS or use IP address instead of hostname"
        elif "Connection refused" in error_msg:
            error_msg += " → Check if Proxmox host is reachable and ports 22/8006 are open"
        elif "Authentication" in error_msg or "permission" in error_msg.lower():
            error_msg += " → Check API token and SSH credentials"
        elif "timeout" in error_msg.lower():
            error_msg += " → Network timeout, check firewall and connectivity"
        elif "Missing required Proxmox host commands" in error_msg:
            error_msg += " → Enable SSH mode or install Proxmox CLI tools"
        
        messages.error(request, f"❌ Connection to '{host.label}' failed: {error_msg}")
        
        # Log the full error for debugging
        logger.error(f"Proxmox connection test failed for {host.label}: {exc}", exc_info=True)
        
    return redirect("dashboard:hosts")


def test_vmware_host(request: HttpRequest, host_id: int) -> HttpResponse:
    from vmware_to_proxmox.vmware import VmwareClient
    host = get_object_or_404(VMwareHost, pk=host_id)
    client = VmwareClient(
        host=host.host,
        username=host.username,
        password=host.password,
        port=host.port,
        allow_insecure_ssl=host.allow_insecure_ssl,
    )
    try:
        with client:
            vms = client.list_vms()
        messages.success(request, f"\u2713 Connected to '{host.label}': {len(vms)} VM(s) visible.")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"\u2717 Connection to '{host.label}' failed: {exc}")
    return redirect("dashboard:hosts")
