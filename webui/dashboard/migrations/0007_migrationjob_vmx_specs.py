from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0006_migrationjob_host_fks"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="vmx_specs",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
