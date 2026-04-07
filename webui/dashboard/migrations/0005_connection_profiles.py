from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0004_disk_storage_map_nic_bridge_map"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProxmoxHost",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("label", models.CharField(max_length=255, unique=True, help_text="Friendly name, e.g. 'pve-prod'")),
                ("node", models.CharField(max_length=255, help_text="Proxmox node name")),
                ("api_host", models.CharField(max_length=255, blank=True, default="")),
                ("api_user", models.CharField(max_length=255, default="root@pam")),
                ("api_token_name", models.CharField(max_length=255, blank=True, default="")),
                ("api_token_value", models.CharField(max_length=512, blank=True, default="")),
                ("api_verify_ssl", models.BooleanField(default=False)),
                ("ssh_enabled", models.BooleanField(default=True)),
                ("ssh_host", models.CharField(max_length=255, blank=True, default="")),
                ("ssh_port", models.IntegerField(default=22)),
                ("ssh_username", models.CharField(max_length=64, default="root")),
                ("ssh_password", models.CharField(max_length=512, blank=True, default="")),
                ("ssh_private_key", models.TextField(blank=True, default="")),
                ("default_storage", models.CharField(max_length=255, blank=True, default="")),
                ("default_bridge", models.CharField(max_length=255, blank=True, default="vmbr0")),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="VMwareHost",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("label", models.CharField(max_length=255, unique=True, help_text="Friendly name, e.g. 'vcenter-prod'")),
                ("host", models.CharField(max_length=255, help_text="vCenter / ESXi hostname or IP")),
                ("username", models.CharField(max_length=255, default="root")),
                ("password", models.CharField(max_length=512, blank=True, default="")),
                ("port", models.IntegerField(default=443)),
                ("allow_insecure_ssl", models.BooleanField(default=True)),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
