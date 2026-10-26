from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("xhs_download", "0031_separate_download_audit_events"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerhealthtestrun",
            name="shadow_details",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="providerhealthtestsetting",
            name="public_shadow_enabled",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.CreateModel(
            name="PipelineShadowDailyCount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField()),
                ("platform", models.CharField(max_length=64)),
                ("provider", models.CharField(max_length=80)),
                ("difference_code", models.CharField(max_length=120)),
                ("count", models.PositiveBigIntegerField(default=0)),
            ],
            options={
                "verbose_name": "管线影子每日计数",
                "verbose_name_plural": "管线影子每日计数",
                "ordering": ("-date",),
                "constraints": [models.UniqueConstraint(
                    fields=("date", "platform", "provider", "difference_code"),
                    name="pipeline_shadow_daily_unique",
                )],
            },
        ),
    ]
