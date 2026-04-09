from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0007_migrationjob_vmx_specs"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="vmid",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
