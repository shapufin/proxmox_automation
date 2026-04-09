from __future__ import annotations

from django.db import models


class ProxmoxHost(models.Model):
    label           = models.CharField(max_length=255, unique=True)
    node            = models.CharField(max_length=255)
    api_host        = models.CharField(max_length=255, blank=True, default="")
    api_user        = models.CharField(max_length=255, default="root@pam")
    api_token_name  = models.CharField(max_length=255, blank=True, default="")
    api_token_value = models.CharField(max_length=512, blank=True, default="")
    api_verify_ssl  = models.BooleanField(default=False)
    ssh_enabled     = models.BooleanField(default=True)
    ssh_host        = models.CharField(max_length=255, blank=True, default="")
    ssh_port        = models.IntegerField(default=22)
    ssh_username    = models.CharField(max_length=64, default="root")
    ssh_password    = models.CharField(max_length=512, blank=True, default="")
    ssh_private_key = models.TextField(blank=True, default="")
    default_storage = models.CharField(max_length=255, blank=True, default="")
    default_bridge  = models.CharField(max_length=255, blank=True, default="vmbr0")
    notes           = models.TextField(blank=True, default="")
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self) -> str:
        return self.label


class VMwareHost(models.Model):
    label              = models.CharField(max_length=255, unique=True)
    host               = models.CharField(max_length=255)
    username           = models.CharField(max_length=255, default="root")
    password           = models.CharField(max_length=512, blank=True, default="")
    port               = models.IntegerField(default=443)
    allow_insecure_ssl = models.BooleanField(default=True)
    notes              = models.TextField(blank=True, default="")
    created_at         = models.DateTimeField(auto_now_add=True)
    updated_at         = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self) -> str:
        return self.label


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
    vmid = models.IntegerField(null=True, blank=True)
    manifest_path = models.CharField(max_length=1024, blank=True, default="")
    source_paths = models.JSONField(default=list, blank=True)
    storage = models.CharField(max_length=255, blank=True, default="")
    bridge = models.CharField(max_length=255, blank=True, default="")
    disk_format = models.CharField(max_length=16, blank=True, default="")
    proxmox_host = models.ForeignKey("ProxmoxHost", null=True, blank=True, on_delete=models.SET_NULL)
    vmware_host  = models.ForeignKey("VMwareHost",  null=True, blank=True, on_delete=models.SET_NULL)
    disk_storage_map = models.JSONField(default=dict, blank=True)
    nic_bridge_map = models.JSONField(default=dict, blank=True)
    disk_resize_map = models.JSONField(default=dict, blank=True)
    allow_disk_shrink = models.BooleanField(default=False)
    fallback_nic_bridge = models.CharField(max_length=255, blank=True, default="")
    vmx_specs = models.JSONField(default=dict, blank=True)
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
