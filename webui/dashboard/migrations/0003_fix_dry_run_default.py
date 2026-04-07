from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0002_add_config_profile"),
    ]

    operations = [
        migrations.AlterField(
            model_name="migrationjob",
            name="dry_run",
            field=models.BooleanField(default=True),
        ),
    ]
