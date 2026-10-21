from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("xhs_download", "0028_evaluation_history"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerhealthtestrun",
            name="download_smoke_details",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
