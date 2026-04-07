from __future__ import annotations

from django.db import models


class JobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class MigrationMode(models.TextChoices):
    VMWARE = "vmware", "VMware direct"
    LOCAL = "local", "Local disks"


class MigrationJob(models.Model):
    name = models.CharField(max_length=255)
    mode = models.CharField(max_length=20, choices=MigrationMode.choices, default=MigrationMode.LOCAL)
    config_profile = models.CharField(max_length=255, blank=True, default="")
    vm_name = models.CharField(max_length=255, blank=True, default="")
    manifest_path = models.CharField(max_length=1024, blank=True, default="")
    source_paths = models.JSONField(default=list, blank=True)
    storage = models.CharField(max_length=255, blank=True, default="")
    bridge = models.CharField(max_length=255, blank=True, default="")
    disk_format = models.CharField(max_length=16, blank=True, default="")
    dry_run = models.BooleanField(default=True)
    start_after_import = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=JobStatus.choices, default=JobStatus.PENDING)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    logs = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"
