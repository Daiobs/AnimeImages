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

from xhs_download import views as download_views

urlpatterns = [
                  path("", download_views.search_view, {"platform_key": "home"}, name="home"),
                  path("results/", download_views.search_results_view, {"platform_key": "home"}, name="home_results"),
                  path("xhs/", download_views.search_view, {"platform_key": "xhs"}, name="xhs_home"),
                  path("xhs/results/", download_views.search_results_view, {"platform_key": "xhs"}, name="xhs_results"),
                  path("tiktok/", download_views.search_view, {"platform_key": "tiktok"}, name="tiktok_home"),
                  path("tiktok/results/", download_views.search_results_view, {"platform_key": "tiktok"}, name="tiktok_results"),
                  path("bilibili/", download_views.search_view, {"platform_key": "bilibili"}, name="bilibili_home"),
                  path("bilibili/results/", download_views.search_results_view, {"platform_key": "bilibili"}, name="bilibili_results"),
                  path("media/proxy-image/", download_views.proxy_image_view, name="media_proxy_image"),
                  path("media/download-images/", download_views.download_images_api, name="media_download_images_api"),
                  path("library/", include("search_app.urls")),
                  path("admin/", admin.site.urls),
                  path("xhs_download/", RedirectView.as_view(url="/xhs/", permanent=False), name="legacy_xhs_redirect"),
                  path('xhs_download/', include('xhs_download.urls')),
                  path('submissions/', include('submissions.urls')),
              ] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
