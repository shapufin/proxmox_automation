from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0003_fix_dry_run_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="disk_storage_map",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="migrationjob",
            name="nic_bridge_map",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
