# Generated migration to add disk_resize_map field
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0008_migrationjob_vmid"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="disk_resize_map",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
