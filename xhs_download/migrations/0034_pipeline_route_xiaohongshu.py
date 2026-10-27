from django.db import migrations


XIAOHONGSHU_ROUTE = {
    "platform": "xiaohongshu",
    "enabled": False,
    "order": ["tiantianjiexi", "kukutool", "native"],
    "policy": "floor_and_upgrade",
    "budget_seconds": 8.0,
    "floor_provider": "native",
    "upgrade_window_seconds": 8.0,
    "platform_names": ["小红书", "xiaohongshu", "RedNote"],
}


def add_route(apps, schema_editor):
    PipelineRoute = apps.get_model("xhs_download", "PipelineRoute")
    PipelineRoute.objects.get_or_create(
        platform=XIAOHONGSHU_ROUTE["platform"],
        defaults={key: value for key, value in XIAOHONGSHU_ROUTE.items() if key != "platform"},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("xhs_download", "0033_pipeline_route"),
    ]

    operations = [
        migrations.RunPython(add_route, migrations.RunPython.noop),
    ]
