from django.apps import AppConfig


class XhsDownloadConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "xhs_download"

    def ready(self):
        from . import checks  # noqa: F401
        from .services.platforms.douyin_kukutool import initialize_kukutool_runtime

        initialize_kukutool_runtime()
