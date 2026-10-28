from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("xhs_download", "0035_pipeline_route_bilibili"),
    ]

    operations = [
        migrations.AddField(
            model_name="pipelineroute",
            name="live_photo_mode",
            field=models.CharField(
                choices=[
                    ("livephoto", "实况照片：照片和动态一起保存"),
                    ("separate", "照片和视频分开"),
                    ("still_only", "只保存照片"),
                ],
                default="livephoto",
                help_text="目前只对小红书生效；只在 Native 给出同一项的照片和动态时按实况照片展示。",
                max_length=16,
                verbose_name="实况保存方式",
            ),
        ),
    ]
