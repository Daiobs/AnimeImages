from xhs_download.models import ProviderHealthTestRun, ProviderRuntimeSetting


class ProviderRuntimeSettingProxy(ProviderRuntimeSetting):
    class Meta:
        proxy = True
        verbose_name = "Provider 运行配置"
        verbose_name_plural = "Provider 运行配置"


class ProviderHealthTestPanelProxy(ProviderHealthTestRun):
    class Meta:
        proxy = True
        verbose_name = "Provider 健康测试"
        verbose_name_plural = "Provider 健康测试"
