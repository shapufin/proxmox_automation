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
]
