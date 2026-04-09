from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0009_migrationjob_disk_resize_map"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="allow_disk_shrink",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="migrationjob",
            name="fallback_nic_bridge",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
