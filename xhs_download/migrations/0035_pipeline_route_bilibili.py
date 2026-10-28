from django.db import migrations


BILIBILI_ROUTE = {
    "platform": "bilibili",
    "enabled": False,
    # Native first; TianTian is the existing availability fallback.
    "order": ["native", "tiantianjiexi"],
    "policy": "first_success",
    "budget_seconds": 8.0,
    "floor_provider": "",
    "upgrade_window_seconds": 8.0,
    "platform_names": ["哔哩哔哩", "bilibili", "B站"],
}


def add_route(apps, schema_editor):
    PipelineRoute = apps.get_model("xhs_download", "PipelineRoute")
    PipelineRoute.objects.get_or_create(
        platform=BILIBILI_ROUTE["platform"],
        defaults={key: value for key, value in BILIBILI_ROUTE.items() if key != "platform"},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("xhs_download", "0034_pipeline_route_xiaohongshu"),
    ]

    operations = [
        migrations.RunPython(add_route, migrations.RunPython.noop),
    ]
