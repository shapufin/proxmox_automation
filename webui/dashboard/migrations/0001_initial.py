# Generated manually for the initial dashboard job model.
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="MigrationJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("mode", models.CharField(choices=[("vmware", "VMware direct"), ("local", "Local disks")], default="local", max_length=20)),
                ("vm_name", models.CharField(blank=True, default="", max_length=255)),
                ("manifest_path", models.CharField(blank=True, default="", max_length=1024)),
                ("source_paths", models.JSONField(blank=True, default=list)),
                ("storage", models.CharField(blank=True, default="", max_length=255)),
                ("bridge", models.CharField(blank=True, default="", max_length=255)),
                ("disk_format", models.CharField(blank=True, default="", max_length=16)),
                ("dry_run", models.BooleanField(default=False)),
                ("start_after_import", models.BooleanField(default=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("running", "Running"), ("succeeded", "Succeeded"), ("failed", "Failed")], default="pending", max_length=20)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True, default="")),
                ("logs", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
