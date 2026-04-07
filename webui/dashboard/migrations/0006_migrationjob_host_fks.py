from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0005_connection_profiles"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="proxmox_host",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="dashboard.proxmoxhost",
            ),
        ),
        migrations.AddField(
            model_name="migrationjob",
            name="vmware_host",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="dashboard.vmwarehost",
            ),
        ),
    ]
