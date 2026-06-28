from xhs_download.models import ProviderRuntimeSetting


class ProviderRuntimeSettingProxy(ProviderRuntimeSetting):
    class Meta:
        proxy = True
        verbose_name = "Provider 运行配置"
        verbose_name_plural = "Provider 运行配置"
