import django.core.validators
from django.db import migrations, models


DOUYIN_ROUTE = {
    "platform": "douyin",
    "enabled": False,
    "order": ["tiantianjiexi", "kukutool", "native"],
    "policy": "floor_and_upgrade",
    "budget_seconds": 8.0,
    "floor_provider": "native",
    "upgrade_window_seconds": 8.0,
    "platform_names": ["抖音", "douyin"],
}


def add_default_routes(apps, schema_editor):
    PipelineRoute = apps.get_model("xhs_download", "PipelineRoute")
    PipelineRoute.objects.get_or_create(
        platform=DOUYIN_ROUTE["platform"],
        defaults={key: value for key, value in DOUYIN_ROUTE.items() if key != "platform"},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("xhs_download", "0032_pipeline_shadow_storage"),
    ]

    operations = [
        migrations.CreateModel(
            name="PipelineRoute",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("platform", models.CharField(max_length=64, unique=True, verbose_name="平台")),
                ("enabled", models.BooleanField(default=False, verbose_name="使用统一管线")),
                ("order", models.JSONField(blank=True, default=list, verbose_name="Provider 顺序")),
                ("policy", models.CharField(
                    choices=[("first_success", "first_success：第一个成功的结果"),
                             ("floor_and_upgrade", "floor_and_upgrade：保底并行，首选优先")],
                    default="first_success", max_length=24, verbose_name="策略")),
                ("budget_seconds", models.FloatField(
                    default=8.0,
                    validators=[django.core.validators.MinValueValidator(1.0),
                                django.core.validators.MaxValueValidator(30.0)],
                    verbose_name="前台预算（秒）")),
                ("floor_provider", models.CharField(blank=True, default="", max_length=64, verbose_name="保底 Provider")),
                ("upgrade_window_seconds", models.FloatField(
                    default=8.0, help_text="不小于前台预算；当前请求在窗口结束前给出最终结果。",
                    validators=[django.core.validators.MinValueValidator(1.0),
                                django.core.validators.MaxValueValidator(60.0)],
                    verbose_name="升级窗口（秒）")),
                ("platform_names", models.JSONField(blank=True, default=list, verbose_name="平台显示名")),
                ("cooldown_base_seconds", models.PositiveIntegerField(
                    default=10, help_text="Provider 返回 unavailable 后的冷却，按连续失败次数翻倍。",
                    validators=[django.core.validators.MinValueValidator(1),
                                django.core.validators.MaxValueValidator(600)],
                    verbose_name="冷却基础时长（秒）")),
                ("cooldown_max_seconds", models.PositiveIntegerField(
                    default=300, help_text="内核另有 1 小时硬上限；成功一次即清零。",
                    validators=[django.core.validators.MinValueValidator(1),
                                django.core.validators.MaxValueValidator(3600)],
                    verbose_name="冷却上限（秒）")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "统一管线路由",
                "verbose_name_plural": "统一管线路由",
                "ordering": ("platform",),
            },
        ),
        migrations.RunPython(add_default_routes, migrations.RunPython.noop),
    ]
