from __future__ import annotations

from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.dashboard, name="index"),
    path("wizard/", views.wizard, name="wizard"),
    path("browse/", views.browse_disks, name="browse"),
    path("launch/", views.launch_job, name="launch"),
    path("configs/", views.config_profile_editor, name="config_profiles"),
    path("configs/save/", views.save_profile, name="save_profile"),
    path("jobs/<int:job_id>/", views.job_detail, name="job_detail"),
    path("jobs/<int:job_id>/run/", views.run_pending_job, name="run_pending_job"),
    path("api/proxmox-status/", views.proxmox_status, name="proxmox_status"),
    path("api/vmware-vms/", views.vmware_vms, name="vmware_vms"),
    path("api/browse-directory/", views.browse_directory, name="browse_directory"),
    path("api/vmdk-scan/", views.vmdk_scan, name="vmdk_scan"),
    path("api/peek-archive/", views.peek_archive, name="peek_archive"),
    # Host management
    path("hosts/", views.hosts, name="hosts"),
    path("hosts/proxmox/save/", views.save_proxmox_host, name="save_proxmox_host"),
    path("hosts/proxmox/<int:host_id>/edit/", views.edit_proxmox_host, name="edit_proxmox_host"),
    path("hosts/proxmox/<int:host_id>/delete/", views.delete_proxmox_host, name="delete_proxmox_host"),
    path("hosts/proxmox/<int:host_id>/test/", views.test_proxmox_host, name="test_proxmox_host"),
    path("hosts/vmware/save/", views.save_vmware_host, name="save_vmware_host"),
    path("hosts/vmware/<int:host_id>/edit/", views.edit_vmware_host, name="edit_vmware_host"),
    path("hosts/vmware/<int:host_id>/delete/", views.delete_vmware_host, name="delete_vmware_host"),
    path("hosts/vmware/<int:host_id>/test/", views.test_vmware_host, name="test_vmware_host"),
]
