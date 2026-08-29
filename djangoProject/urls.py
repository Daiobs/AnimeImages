"""
URL configuration for djangoProject project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.conf import settings
from django.urls import path, include
from django.conf.urls.static import static
from django.views.generic import RedirectView

from djangoProject import health
from xhs_download import views as download_views
from xhs_download import user_feedback_views

urlpatterns = [
                  path(
                      "favicon.ico",
                      RedirectView.as_view(
                          url=f"/{settings.STATIC_URL.lstrip('/')}brand/favicon-32.png",
                          permanent=True,
                      ),
                      name="favicon",
                  ),
                  path("health/live/", health.liveness_view, name="health_live"),
                  path("health/ready/", health.readiness_view, name="health_ready"),
                  path(
                      "feedback/",
                      user_feedback_views.user_feedback_submit_view,
                      name="user_feedback_submit",
                  ),
                  path(
                      "ordinary-media-download-sw.js",
                      download_views.ordinary_media_service_worker_view,
                      name="ordinary_media_service_worker",
                  ),
                  path("admin/control-plane/", download_views.control_plane_view, name="control_plane"),
                  path("admin/provider-settings/", download_views.provider_settings_view, name="provider_settings"),
                  path("admin/policy-engine/", download_views.policy_engine_view, name="policy_engine"),
                  path("admin/policy-control/", download_views.policy_control_view, name="policy_control"),
                  path("admin/control-convergence/", download_views.control_convergence_view, name="control_convergence"),
                  path("admin/stability-v6/", download_views.stability_v6_view, name="stability_v6"),
                  path("admin/autonomous-evolution/", download_views.autonomous_evolution_view, name="autonomous_evolution"),
                  path("admin/governance-v8/", download_views.governance_v8_view, name="governance_v8"),
                  path("admin/console/", download_views.admin_console_view, name="admin_console"),
                  path("admin/release-management/", download_views.release_management_view, name="release_management"),
                  path("admin/rollout-guard/", download_views.rollout_guard_view, name="rollout_guard"),
                  path("admin/provider-health/", download_views.provider_health_view, name="provider_health"),
                  path("admin/provider-health/control/", download_views.provider_control_view, name="provider_control"),
                  path("admin/provider-health-tests/", download_views.provider_health_tests_view, name="provider_health_tests"),
                  path("admin/self-healing/run/", download_views.self_healing_run_view, name="self_healing_run"),
                  path("admin/ops-dashboard/", download_views.ops_dashboard_view, name="ops_dashboard"),
                  path("admin/observability-v2/", download_views.observability_v2_view, name="observability_v2"),
                  path("admin/feedback-loop/", download_views.feedback_loop_view, name="feedback_loop"),
                  path("admin/logs/", download_views.logs_center_view, name="logs_center"),
                  path("admin/policy-center/", download_views.policy_center_view, name="policy_center"),
                  path("admin/parse-logs/", download_views.parse_logs_view, name="parse_logs"),
                  path("admin/download-logs/", download_views.download_logs_view, name="download_logs"),
                  path("admin/provider-audit-logs/", download_views.provider_audit_logs_view, name="provider_audit_logs"),
                  path("", download_views.search_view, {"platform_key": "home"}, name="home"),
                  path("results/", download_views.search_results_view, {"platform_key": "home"}, name="home_results"),
                  path("xhs/", download_views.search_view, {"platform_key": "xhs"}, name="xhs_home"),
                  path("xhs/results/", download_views.search_results_view, {"platform_key": "xhs"}, name="xhs_results"),
                  path("tiktok/", download_views.search_view, {"platform_key": "tiktok"}, name="tiktok_home"),
                  path("tiktok/results/", download_views.search_results_view, {"platform_key": "tiktok"}, name="tiktok_results"),
                  path("bilibili/", download_views.search_view, {"platform_key": "bilibili"}, name="bilibili_home"),
                  path("bilibili/results/", download_views.search_results_view, {"platform_key": "bilibili"}, name="bilibili_results"),
                  path("batch/", download_views.batch_parse_view, name="batch_parse"),
                  path("batch/start/", download_views.batch_parse_start_view, name="batch_parse_start"),
                  path("batch/item/", download_views.batch_parse_item_view, name="batch_parse_item"),
                  path("batch/retry/", download_views.batch_parse_retry_view, name="batch_parse_retry"),
                  path("media/douyin-continuation/", download_views.douyin_continuation_view, name="media_douyin_continuation"),
                  path(
                      "media/browser-download-telemetry/",
                      download_views.browser_download_telemetry_view,
                      name="media_browser_download_telemetry",
                  ),
                  path("media/proxy-image/", download_views.proxy_image_view, name="media_proxy_image"),
                  path(
                      "media/xhs-livephoto-motion-descriptor/",
                      download_views.xhs_livephoto_motion_descriptor_view,
                      name="media_xhs_livephoto_motion_descriptor",
                  ),
                  path(
                      "media/xhs-livephoto-pair-descriptor/",
                      download_views.xhs_livephoto_pair_descriptor_view,
                      name="media_xhs_livephoto_pair_descriptor",
                  ),
                  path("media/download-prepare/", download_views.download_prepare_api, name="media_download_prepare"),
                  path("media/download-stream/<str:token>/", download_views.download_stream_view, name="media_download_stream"),
                  path("media/download-status/<str:token>/", download_views.download_status_view, name="media_download_status"),
                  path("media/download-images/", download_views.download_images_api, name="media_download_images_api"),
                  path("media/download-tasks/", download_views.create_download_task_api, name="media_download_task_create"),
                  path("media/download-tasks/<str:task_id>/", download_views.download_task_status_api, name="media_download_task_status"),
                  path("media/download-tasks/<str:task_id>/file/", download_views.download_task_file_api, name="media_download_task_file"),
                  path("library/", include("search_app.urls")),
                  path("admin/", admin.site.urls),
                  path("xhs_download/", RedirectView.as_view(url="/xhs/", permanent=False), name="legacy_xhs_redirect"),
                  path('xhs_download/', include('xhs_download.urls')),
              ]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
