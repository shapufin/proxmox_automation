from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="config_profile",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
